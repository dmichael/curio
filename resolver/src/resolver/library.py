"""The durability tier and its ledgers: what the box has decided to keep.

The library is source-native: Kubo pins IPFS, Curio's static store keeps
ordinary HTTP/data, and the retained AR.IO registry records native Core
hydration. Ordinary AR.IO warm records remain cache diagnostics, not keep
claims. This module owns the source-appropriate single-target helpers and
`GET /library`, the cross-plane answer to "what does the box actually hold?".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from .arweave_retention import keep_arweave, retained_records
from .config import Settings
from .favorites import get_favorites
from .overrides import get_registry
from .refs import ipfs_parts
from .resolve import Resolved

_STATUS_TIMEOUT = 10.0  # per probe: status must answer even when a plane hangs

_log = logging.getLogger("resolver.library")

# Strong references to fire-and-forget tasks: asyncio keeps only weak ones,
# so an unreferenced task can be garbage-collected mid-flight. Background
# pins (here) and seed jobs (seed.py) share this registry via keep_task.
_TASKS: set[asyncio.Task[None]] = set()


def keep_task(task: asyncio.Task[None]) -> None:
    """Hold a strong reference to a background task until it finishes."""
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


class Ingested(NamedTuple):
    """What ingest_url produced: Kubo's CID for the bytes, plus the
    provenance-grade facts observed while they streamed through."""

    cid: str
    size: int
    sha256: str | None
    content_type: str | None


async def ingest_url(
    client: httpx.AsyncClient,
    url: str,
    settings: Settings,
    *,
    add_params: dict[str, str],
    compute_sha256: bool = False,
) -> Ingested:
    """Stream `url` into Kubo's `add` — the one path HTTP bytes take into
    the library (CID recovery re-adds, plain-HTTP captures).

    The body is buffered through a size-capped tempfile (Kubo's add wants a
    complete file, and the cap — seed_recover_max_bytes — must be enforced
    before anything is added). Raises ValueError on an oversized body and
    lets httpx/JSON/KeyError failures propagate for the caller to map; what
    `add` did with the bytes (pin or not, CID version) is the caller's
    business via add_params.
    """
    digest = hashlib.sha256() if compute_sha256 else None
    size = 0
    with tempfile.TemporaryFile() as buffer:
        async with client.stream("GET", url, timeout=settings.seed_pin_timeout) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type")
            async for chunk in response.aiter_bytes(65536):
                size += len(chunk)
                if size > settings.seed_recover_max_bytes:
                    raise ValueError(f"body exceeds {settings.seed_recover_max_bytes} bytes")
                if digest is not None:
                    digest.update(chunk)
                buffer.write(chunk)
        buffer.seek(0)
        response = await client.post(
            f"{settings.ipfs_api}/api/v0/add",
            params=add_params,
            files={"file": ("ingested", buffer)},
            timeout=settings.seed_pin_timeout,
        )
    response.raise_for_status()
    cid = json.loads(response.text.strip().splitlines()[-1])["Hash"]
    return Ingested(cid, size, digest.hexdigest() if digest else None, content_type)


# --- single-target pinning (favorites, resolve?pin=1) ----------------------
# Resolution alone deliberately never pins — browsing must not grow the
# library. These helpers make ONE resolved target durable when the caller
# declares intent (a favorite, an explicit pin=1). Wallet-wide pinning is a
# seed job, not a loop over these.


async def pin_resolved(
    result: Resolved, settings: Settings, client: httpx.AsyncClient, why: str = "pin"
) -> str | None:
    """Apply explicit keep intent on the source-native storage plane."""
    if not result.resolved:
        return None
    ipfs = ipfs_parts(result.resolved_url)
    if ipfs is not None:
        cid, path = ipfs
        response = await client.post(
            f"{settings.ipfs_api}/api/v0/pin/add",
            params={"arg": f"/ipfs/{cid}{path}"},
            timeout=settings.seed_pin_timeout,
        )
        response.raise_for_status()
        return "pinned"
    if result.provider == "arweave" or result.source_kind == "arweave":
        from .refs import arweave_parts

        arweave = arweave_parts(result.original_ref)
        if arweave is None:
            return "failed"
        txid, path = arweave
        return await keep_arweave(txid, path, settings, client)
    return None


def pin_in_background(
    result: Resolved, settings: Settings, client: httpx.AsyncClient, why: str = "pin"
) -> None:
    """Fire-and-forget pin of one resolved target: the request that asked
    for it must return immediately even when the content is cold and large."""
    keep_task(asyncio.create_task(_pin_logged(result, settings, client, why)))


async def _pin_logged(
    result: Resolved, settings: Settings, client: httpx.AsyncClient, why: str
) -> None:
    try:
        outcome = await pin_resolved(result, settings, client, why=why)
    except Exception as exc:  # best effort: a failed pin must not crash the loop
        _log.warning(
            "%s pin failed for %s: %s: %s", why, result.original_ref, type(exc).__name__, exc
        )
        return
    if outcome:
        _log.info("%s %s: %s", why, result.original_ref, outcome)


def captures_file(settings: Settings) -> Path:
    """One provenance ledger for everything that enters Kubo without a
    canonical content address: seed captures and operator uploads (store.py)."""
    return Path(settings.seed_capture_dir) / "captures.jsonl"


# File I/O around captures (this read, the tempfile buffers in ingest_url and
# the jsonl appends) is deliberately synchronous inside async code: at
# household scale the blocking is microseconds against network-bound work,
# and async-file machinery isn't worth it. Don't "fix" it.
def _captured_sources(settings: Settings) -> set[str]:
    """Source URLs already captured — capture is once per URL, ever."""
    sources: set[str] = set()
    try:
        with open(captures_file(settings)) as fh:
            for line in fh:
                try:
                    source = json.loads(line)["source"]
                except (ValueError, KeyError, TypeError):
                    continue
                if isinstance(source, str):
                    sources.add(source)
    except OSError:
        pass
    return sources


def warmed_file(settings: Settings) -> Path:
    """Ledger of Arweave txids deliberately warmed through the box's gateway.

    ar-io's cache is evictable and exposes no inventory API, so this file is
    the only record of what the box ever *meant* to hold on the Arweave
    plane; /library re-checks each entry against the gateway's X-Cache."""
    return Path(settings.seed_capture_dir) / "warmed.jsonl"


def record_warm(txid: str, settings: Settings, why: str) -> None:
    """Append a successful warm to the ledger — once per txid, ever.

    No-op when capture is disabled. Same deliberately-synchronous file I/O
    as the captures ledger — see the note above _captured_sources."""
    if not settings.seed_capture_dir:
        return
    if txid in warmed_txids(settings):
        return
    record = {
        "txid": txid,
        "warmed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "why": why,
    }
    path = warmed_file(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(record) + "\n")


def warmed_txids(settings: Settings) -> list[str]:
    """Ordered unique txids from the warm ledger; [] when disabled/missing."""
    if not settings.seed_capture_dir:
        return []
    txids: dict[str, None] = {}  # ordered de-dupe
    try:
        with open(warmed_file(settings)) as fh:
            for line in fh:
                try:
                    txid = json.loads(line)["txid"]
                except (ValueError, KeyError, TypeError):
                    continue
                if isinstance(txid, str):
                    txids[txid] = None
    except OSError:
        pass
    return list(txids)


async def library_status(settings: Settings, client: httpx.AsyncClient) -> dict[str, Any]:
    """What the box actually holds, plane by plane.

    IPFS pins are durable. The Arweave retained registry counts native Core
    hydration outcomes; ordinary warm records are separately replayed against
    Envoy's X-Cache for cache diagnostics only. A cache miss is degraded,
    never evidence for substituting a retained identity.
    Each plane degrades to {"error": …} on its own; status never 500s
    because one backend is down.
    """
    return {
        "ipfs": await _ipfs_status(settings, client),
        "arweave": await _arweave_status(settings, client),
        "registry": _registry_status(settings),
    }


async def _ipfs_status(settings: Settings, client: httpx.AsyncClient) -> dict[str, Any]:
    try:
        # type=recursive counts the pin heads — the library's units. type=all
        # would enumerate every indirect block of every DAG.
        pins = await client.post(
            f"{settings.ipfs_api}/api/v0/pin/ls",
            params={"type": "recursive"},
            timeout=_STATUS_TIMEOUT,
        )
        pins.raise_for_status()
        stat = await client.post(f"{settings.ipfs_api}/api/v0/repo/stat", timeout=_STATUS_TIMEOUT)
        stat.raise_for_status()
        repo = stat.json()
        return {
            "pinned": len(pins.json().get("Keys") or {}),
            "repo_size_bytes": repo.get("RepoSize"),
            "repo_objects": repo.get("NumObjects"),
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


async def _arweave_status(settings: Settings, client: httpx.AsyncClient) -> dict[str, Any]:
    retained = retained_records(settings)
    txids = warmed_txids(settings)
    if not txids:
        return {
            "retained": {
                "kept": sum(record["state"] == "kept" for record in retained),
                "pending": sum(record["state"] == "pending" for record in retained),
                "failed": sum(record["state"] == "failed" for record in retained),
                "operation": "isolated native retained-plane operation (not an AR.IO r81 pin API)",
            },
            "known_warmed": 0,
            "currently_cached": 0,
        }
    sem = asyncio.Semaphore(settings.seed_concurrency)

    async def cached(txid: str) -> bool:
        # X-Cache HIT/MISS on GET/HEAD is the only cache introspection ar-io
        # offers. A failed HEAD counts as not-cached: it isn't servable now.
        async with sem:
            try:
                response = await client.head(
                    f"{settings.arweave_internal}/{txid}", timeout=_STATUS_TIMEOUT
                )
                return response.headers.get("x-cache", "").upper().startswith("HIT")
            except httpx.HTTPError:
                return False

    hits = sum(await asyncio.gather(*(cached(txid) for txid in txids)))
    status: dict[str, Any] = {
        "retained": {
            "kept": sum(record["state"] == "kept" for record in retained),
            "pending": sum(record["state"] == "pending" for record in retained),
            "failed": sum(record["state"] == "failed" for record in retained),
            "operation": "isolated native retained-plane operation (not an AR.IO r81 pin API)",
        },
        "known_warmed": len(txids), "currently_cached": hits,
    }
    if hits != len(txids):
        status["note"] = (
            "ar-io cache is evictable — currently_cached < known_warmed means evictions"
        )
    return status


def _registry_status(settings: Settings) -> dict[str, Any]:
    """Counts of the operator-state files; None marks a disabled subsystem."""
    captures = None
    if settings.seed_capture_dir:
        try:
            with open(captures_file(settings)) as fh:
                captures = sum(1 for _ in fh)
        except OSError:
            captures = 0  # enabled but nothing captured yet
    return {
        "overrides": (
            len(get_registry(settings.overrides_path).entries())
            if settings.overrides_path
            else None
        ),
        "favorites": (
            len(get_favorites(settings.favorites_path).list_favorites())
            if settings.favorites_path
            else None
        ),
        "captures": captures,
    }

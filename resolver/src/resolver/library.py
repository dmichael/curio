"""Source-native storage helpers and cross-plane library status.

Kubo pins IPFS, Curio stores ordinary HTTP/data, and AR.IO Core eagerly fetches
and verifies Arweave. This module also owns `GET /library`, the cross-plane
answer to "what does the box hold?".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from typing import Any, NamedTuple

import httpx

from .arweave_cache import store_arweave
from .config import Settings
from .favorites import get_favorites
from .overrides import get_registry
from .refs import ipfs_parts
from .resolve import Resolved
from .safe_fetch import safe_stream
from .static_store import StaticStore

_STATUS_TIMEOUT = 10.0  # per probe: status must answer even when a plane hangs

# Strong references to seed tasks: asyncio keeps only weak ones, so an
# unreferenced background job can be garbage-collected mid-flight.
_TASKS: set[asyncio.Task[None]] = set()


def track_task(task: asyncio.Task[None]) -> None:
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
        async with safe_stream(client, "GET", url, settings, timeout=settings.seed_pin_timeout) as response:
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


# --- single-target source storage -----------------------------------------
# POST /resolve stores one final native artifact. Wallet-wide storage remains
# a seed job rather than a client-side loop over this helper.


async def store_resolved(
    result: Resolved, settings: Settings, client: httpx.AsyncClient
) -> str | None:
    """Store one resolved artifact on its source-native storage plane.

    Runtime HTML is pinned or fetched like any artifact. Its public resolution
    status comes from the markup audit in resolve: relative-only bundles are
    ``ready``, externally-referencing shells stay ``live-dependent``.
    """
    if not result.resolved:
        return None
    # The public resolved URL may be a Curio proxy and original_ref may be a
    # metadata document. Retention always targets the final native artifact.
    # The fallback only supports older in-process callers that construct a
    # lightweight result object; every Resolved instance carries final_ref.
    final_ref = getattr(result, "final_ref", None) or result.original_ref
    ipfs = ipfs_parts(final_ref) if final_ref else None
    if ipfs is not None:
        cid, path = ipfs
        response = await client.post(
            f"{settings.ipfs_api}/api/v0/pin/add",
            # Pin the canonical root. A path remains result provenance and
            # serving detail, but a recursive pin of it must not omit siblings.
            params={"arg": f"/ipfs/{cid}"},
            timeout=settings.seed_pin_timeout,
        )
        response.raise_for_status()
        return "pinned"
    if getattr(result, "source_kind", None) == "arweave" or getattr(result, "provider", None) == "arweave":
        from .refs import arweave_parts

        arweave = arweave_parts(final_ref) if final_ref else None
        if arweave is None:
            return "failed"
        txid, path = arweave
        return await store_arweave(txid, path, settings, client)
    return None


async def library_status(settings: Settings, client: httpx.AsyncClient) -> dict[str, Any]:
    """What the box holds, plane by plane.

    IPFS pins are durable. Arweave records are same-Core cache diagnostics:
    resolution, storage, and playback all populate the one persistent Core
    cache. Each plane degrades independently rather than making status
    fail because one backend is down.
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


def _arweave_txids(settings: Settings) -> list[str]:
    """Registered Arweave txids: the catalogue's resolutions, not the cache.

    A reference counts once it has a live playback route — the txid segment
    right after `/arweave/` in media_path, deduped, failures excluded.
    """
    store = StaticStore(settings.static_root, settings.static_cache_max_bytes)
    txids: dict[str, None] = {}  # ordered de-dupe
    for media_path in store.arweave_media_paths():
        # The identity segment may carry a minted display extension
        # (/arweave/<txid>.png); txids never contain dots, so strip from the
        # first dot to recover the txid Core actually knows.
        txid = media_path[len("/arweave/"):].split("/", 1)[0].split(".", 1)[0]
        if txid:
            txids[txid] = None
    return list(txids)


async def _arweave_status(settings: Settings, client: httpx.AsyncClient) -> dict[str, Any]:
    txids = _arweave_txids(settings)
    if not txids:
        return {"known_warmed": 0, "currently_cached": 0}
    sem = asyncio.Semaphore(settings.seed_concurrency)

    async def cached(txid: str) -> bool:
        # X-Cache is the only Core cache introspection. A cold read has its
        # own timeout because Core can retrieve data on demand.
        async with sem:
            try:
                response = await client.head(
                    f"{settings.arweave_internal}/{txid}", timeout=settings.arweave_cold_timeout
                )
                return response.headers.get("x-cache", "").upper().startswith("HIT")
            except httpx.HTTPError:
                return False

    hits = sum(await asyncio.gather(*(cached(txid) for txid in txids)))
    return {
        "known_warmed": len(txids),
        "currently_cached": hits,
        "operation": "same persistent Core cache; local serving is not Arweave-network replication",
    }


def _registry_status(settings: Settings) -> dict[str, Any]:
    """Counts of the operator-state files; None marks a disabled subsystem."""
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
    }

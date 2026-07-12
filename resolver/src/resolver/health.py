"""Gateway health and library status, shared by the REST and MCP surfaces."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .config import Settings
from .favorites import get_favorites
from .overrides import get_registry
from .seed import captures_file, warmed_txids

_STATUS_TIMEOUT = 10.0  # per probe: status must answer even when a plane hangs


async def gateway_health(settings: Settings, client: httpx.AsyncClient) -> dict[str, Any]:
    """Reachability of the box's own gateways.

    `ok` means the gateway answered with a non-5xx status — Kubo's gateway
    root returns 404 when perfectly healthy, so success-status semantics
    would report a healthy backend as down.
    """
    backends: dict[str, Any] = {}
    for name, base in (("ipfs", settings.ipfs_internal), ("arweave", settings.arweave_internal)):
        try:
            response = await client.get(base, timeout=3.0)
            backends[name] = {"ok": response.status_code < 500, "status": response.status_code}
        except httpx.HTTPError as exc:
            backends[name] = {"ok": False, "error": str(exc)}
    return {"healthy": all(b["ok"] for b in backends.values()), "backends": backends}


async def library_status(settings: Settings, client: httpx.AsyncClient) -> dict[str, Any]:
    """What the box actually holds, plane by plane.

    IPFS pins are the durable tier (a pin survives GC). The ar-io cache is
    evictable and has no inventory endpoint, so the arweave plane replays the
    warm ledger (seed.warmed_file) against the gateway's X-Cache header —
    currently_cached below known_warmed means evictions, not an error.
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
    txids = warmed_txids(settings)
    if not txids:
        return {"known_warmed": 0, "currently_cached": 0}
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
    status: dict[str, Any] = {"known_warmed": len(txids), "currently_cached": hits}
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

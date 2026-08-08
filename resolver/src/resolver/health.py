"""Gateway reachability — the /healthz answer, shared by the REST and MCP
surfaces. Library/holdings status lives in library.py."""

from __future__ import annotations

from typing import Any

import httpx

from .config import Settings


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
    # A running process is not evidence of network contribution. Kubo's API
    # can expose observed public swarm addresses; r81 has no equivalent
    # reachability/served-data signal, so status remains explicitly unknown.
    ipfs_participation: dict[str, Any] = {"status": "unreachable"}
    try:
        identity = await client.post(f"{settings.ipfs_api}/api/v0/id", timeout=3.0)
        identity.raise_for_status()
        addresses = identity.json().get("Addresses") or []
        public = [a for a in addresses if "/ip4/127." not in a and "/ip4/0.0.0.0" not in a]
        ipfs_participation = {"status": "reachable" if public else "not_reachable", "addresses": public}
    except (httpx.HTTPError, ValueError):
        pass
    return {
        "healthy": all(b["ok"] for b in backends.values()),
        "backends": backends,
        "participation": {
            "ipfs": ipfs_participation,
            "arweave": {"status": "unknown", "reason": "AR.IO r81 exposes no public reachability evidence"},
        },
    }

"""Gateway reachability — the /healthz answer, shared by the REST and MCP
surfaces. Library/holdings status lives in library.py."""

from __future__ import annotations

import ipaddress
import re
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
    for name, base in (
        ("ipfs", settings.ipfs_internal),
        ("arweave", settings.arweave_internal),
        ("arweave_retained", settings.arweave_retained_internal),
    ):
        try:
            response = await client.get(base, timeout=3.0)
            backends[name] = {"ok": response.status_code < 500, "status": response.status_code}
        except httpx.HTTPError as exc:
            backends[name] = {"ok": False, "error": str(exc)}
    # A running process is not evidence of network contribution. Kubo's API
    # can expose observed public swarm addresses; r81 has no equivalent
    # reachability/served-data signal, so status remains explicitly unknown.
    ipfs_participation: dict[str, Any] = {"status": "unknown", "reason": "no public reachability evidence"}
    try:
        identity = await client.post(f"{settings.ipfs_api}/api/v0/id", timeout=3.0)
        identity.raise_for_status()
        addresses = identity.json().get("Addresses") or []
        public = []
        for multiaddr in addresses:
            match = re.search(r"/ip(?:4|6)/([^/]+)", str(multiaddr))
            if not match:
                continue
            try:
                if ipaddress.ip_address(match.group(1)).is_global:
                    public.append(multiaddr)
            except ValueError:
                continue
        # Announcing a globally-routable address is useful evidence, but it
        # does not prove inbound reachability from the public swarm.
        ipfs_participation = {
            "status": "unknown",
            "observed_public_addresses": public,
            "reason": "Kubo id reports advertised addresses, not an inbound reachability probe",
        }
    except (httpx.HTTPError, ValueError):
        pass
    return {
        "healthy": all(b["ok"] for b in backends.values()),
        "backends": backends,
        "participation": {
            "ipfs": ipfs_participation,
            "arweave": {"status": "unknown", "reason": "AR.IO r81 exposes no public reachability evidence"},
            "arweave_retained": {"status": "unknown", "reason": "private retained Core has no public reachability role"},
        },
    }

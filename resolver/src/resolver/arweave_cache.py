"""Same-Core AR.IO fetch and cache verification helpers.

AR.IO Core has no Curio pin API.  An explicit keep fully reads the requested
transaction/path through the one persistent Core, then fully reads it again and
requires Core's native ``X-Cache: HIT`` response.  This proves local serving at
that time; it is not a claim of replication into the Arweave network.
"""
from __future__ import annotations

import httpx

from .config import Settings


def _url(txid: str, path: str, settings: Settings) -> str:
    return f"{settings.arweave_internal.rstrip('/')}/{txid}{path}"


async def _consume(client: httpx.AsyncClient, url: str, timeout: float) -> httpx.Headers:
    async with client.stream("GET", url, timeout=timeout) as response:
        response.raise_for_status()
        async for _ in response.aiter_bytes(65536):
            pass
        return response.headers


async def keep_arweave(txid: str, path: str, settings: Settings, client: httpx.AsyncClient) -> str:
    """Eagerly fetch an exact identity and verify its same-Core cache hit."""
    try:
        await _consume(client, _url(txid, path, settings), settings.arweave_cold_timeout)
        headers = await _consume(client, _url(txid, path, settings), settings.arweave_cold_timeout)
    except (httpx.HTTPError, ValueError):
        return "failed"
    return "kept" if headers.get("x-cache", "").strip().lower() == "hit" else "failed"

"""DNS-pinned, redirect-validating HTTP streams for untrusted URLs."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from contextlib import asynccontextmanager
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

from .config import Settings


def external_url_ok(url: str) -> bool:
    """Reject non-HTTP and obvious local/private user-supplied URLs."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def _is_internal_gateway(url: str, settings: Settings) -> bool:
    return any(url == base.rstrip("/") or url.startswith(base.rstrip("/") + "/") for base in (
        settings.ipfs_internal, settings.arweave_internal, settings.arweave_retained_internal,
    ))


def fetch_allowed(url: str, settings: Settings) -> bool:
    """Allow configured local gateways; require every other URL to be public."""
    return _is_internal_gateway(url, settings) or external_url_ok(url)


async def validated_addresses(url: str, settings: Settings) -> list[str] | None:
    """Resolve an external host once and return only safe numeric targets."""
    if not fetch_allowed(url, settings):
        return None
    if _is_internal_gateway(url, settings) or not settings.ssrf_dns_check:
        return []
    parsed = urlparse(url)
    if parsed.hostname is None:
        return None
    try:
        answers = await asyncio.to_thread(
            socket.getaddrinfo, parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        return None
    addresses: list[str] = []
    for _, _, _, _, sockaddr in answers:
        address = ipaddress.ip_address(sockaddr[0])
        if not address.is_global:
            return None
        if str(address) not in addresses:
            addresses.append(str(address))
    return addresses or None


def _pinned_url(url: str, address: str) -> str:
    parsed = urlparse(url)
    host = f"[{address}]" if ":" in address else address
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return urlunparse(parsed._replace(netloc=netloc))


def _host_header(parsed) -> str:
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{parsed.port}" if parsed.port else host


@asynccontextmanager
async def safe_stream(
    client: httpx.AsyncClient, method: str, url: str, settings: Settings, *, timeout: float | None = None,
):
    """Stream an untrusted URL through a DNS-pinned, redirect-safe connection."""
    current = url
    response: httpx.Response | None = None
    for hop in range(settings.redirect_max_hops + 1):
        addresses = await validated_addresses(current, settings)
        if addresses is None:
            raise ValueError("refusing to fetch internal/private URL")
        parsed = urlparse(current)
        if addresses:
            request_url = _pinned_url(current, addresses[0])
            headers = {"host": _host_header(parsed), "connection": "close"}
            extensions = {"sni_hostname": parsed.hostname}
        else:
            request_url, headers, extensions = current, {}, {}
        request = client.build_request(method, request_url, headers=headers, extensions=extensions, timeout=timeout)
        response = await client.send(request, stream=True)
        if response.status_code not in {301, 302, 303, 307, 308}:
            break
        location = response.headers.get("location")
        await response.aclose()
        response = None
        if not location:
            raise ValueError("redirect without Location")
        if hop >= settings.redirect_max_hops:
            raise ValueError("too many redirects")
        current = urljoin(current, location)
    if response is None:
        raise ValueError("redirect failed")
    try:
        yield response
    finally:
        await response.aclose()

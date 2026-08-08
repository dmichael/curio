"""Reference resolution: any media reference -> a box-local playable URL.

Mechanical (no network):
  - ipfs://CID/path, /ipfs/CID/path, https://<any-gw>/ipfs/CID  -> box IPFS gateway
  - ar://txid, https://arweave.net/txid                         -> box Arweave gateway
  - direct http(s) media with a real extension                  -> passthrough

Network (probes go to the INTERNAL gateways so the box's own
pins/cache are used; consumers get the PUBLIC base):
  - extension-less refs -> Content-Type probe -> ?filename=art.<ext> hint (ipfs),
    send-vs-play from the real content type
  - tokenURI JSON       -> animation_url/artifactUri, else largest image by
    Content-Length -> recurse
  - data:application/json -> decoded inline metadata -> recurse
  - verse.works/artworks/... -> scrape tokenUri / iframeUrl / og:image -> recurse

Every step first consults the operator's override registry (overrides.py):
a ref whose canonical content is gone resolves to its recorded replacement,
marked `substituted` with a provenance status — never silently.

Not built: ENS / wallet / tx / contract+tokenId resolution (needs an
RPC/indexer path chosen for the service; see docs/design.md § Open decisions).
"""

from __future__ import annotations

import asyncio
import base64
import html
import ipaddress
import json
import re
import socket
import tempfile
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse, urlunparse

import httpx

from .arweave_retention import retained_available, retained_state
from .config import Settings
from .fixups import (
    KNOWN_EXTENSIONS,
    ext_from_content_type,
    extension_of,
    infer_playback_method,
    probe_headers,
)
from .overrides import get_registry
from .refs import arweave_parts, ipfs_parts
from .static_store import StaticStore

__all__ = ["Resolved", "resolve_ref", "pick_media_field", "external_url_ok"]

_VERSE_HOSTS = {"verse.works", "www.verse.works"}
_META_IMAGE_RE = re.compile(
    r'<meta\s+(?:property|name)="(?:og:image|twitter:image)"\s+content="([^"]+)"',
    re.IGNORECASE,
)
_EMBEDDED_STRING_RE = r'\\"{key}\\":\\"([^\\"]+)\\"'

# animation-tier fields win outright; image-tier fields compete on size.
# artifactUri/displayUri are the TZIP-21 (Tezos) equivalents.
_ANIMATION_FIELDS = ("animation_url", "animationUrl", "artifactUri", "artifact_uri")
_IMAGE_FIELDS = ("image", "image_url", "imageUrl", "displayUri", "display_uri")

_MAX_DEPTH = 4
_MAX_DIR_CHILDREN = 8
# Per event-loop semaphores bound simultaneous remote static downloads. The
# resolver may run tests on multiple loops, hence the loop identity in the key.
_STATIC_FETCH_LIMITERS: dict[tuple[int, int], asyncio.Semaphore] = {}
_STATIC_FETCH_DEPTH: ContextVar[int] = ContextVar("static_fetch_depth", default=0)


@asynccontextmanager
async def _static_fetch_slot(settings: Settings):
    # Metadata can recurse into another HTTP reference. That child belongs to
    # its parent's admission slot rather than waiting for a second one (which
    # would deadlock when every slot is resolving metadata).
    if _STATIC_FETCH_DEPTH.get():
        yield
        return
    loop = asyncio.get_running_loop()
    key = (id(loop), settings.static_fetch_concurrency)
    limiter = _STATIC_FETCH_LIMITERS.setdefault(key, asyncio.Semaphore(settings.static_fetch_concurrency))
    async with limiter:
        token = _STATIC_FETCH_DEPTH.set(1)
        try:
            yield
        finally:
            _STATIC_FETCH_DEPTH.reset(token)


@dataclass
class Resolved:
    original_ref: str
    resolved_url: str
    playback_method: str  # "play" | "send"
    provider: str | None
    resolved: bool
    title: str | None = None
    content_type: str | None = None
    note: str | None = None
    # Override-registry disclosure: replacements are never silent. When the
    # canonical content is gone and an operator-recorded replacement was
    # served, `substituted` is true, `substituted_ref` is the dead canonical
    # ref that matched, and `substitution_status` is the provenance tier
    # (see overrides.STATUSES).
    substituted: bool = False
    substituted_ref: str | None = None
    substitution_status: str | None = None
    source_kind: str | None = None
    # The actual source-native media identity after metadata recursion.  This
    # deliberately differs from original_ref, which remains the caller's
    # discovery input (often a metadata document).
    final_ref: str | None = None
    keep_state: str = "cached"
    integrity: dict[str, str] | None = None

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["source_ref"] = self.final_ref
        # media_url is the product contract; resolved_url remains temporarily
        # for existing API consumers.
        payload["media_url"] = self.resolved_url
        payload["media_type"] = self.content_type
        return payload


def external_url_ok(url: str) -> bool:
    """Refuse obviously-internal destinations in user/metadata-supplied URLs.

    Literal-IP and localhost checks reject obvious local targets; DNS answers
    and every redirect are separately validated before connection.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return True  # a DNS name; accepted (see trust model)
    return addr.is_global


def _is_internal_gateway(url: str, settings: Settings) -> bool:
    return any(url == base.rstrip("/") or url.startswith(base.rstrip("/") + "/")
               for base in (
                   settings.ipfs_internal, settings.arweave_internal,
                   settings.arweave_retained_internal,
               ))


async def _validated_addresses(url: str, settings: Settings) -> list[str] | None:
    """Return DNS answers safe to connect to, rejecting mixed answers too.

    The caller uses one returned numeric address as the TCP target. This is
    intentionally more than a preflight: HTTPX never receives the hostname as
    its connection destination, so a later resolver lookup cannot rebind it.
    """
    if not _fetch_allowed(url, settings):
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
        # is_global rejects RFC1918, loopback, link-local, CGNAT, documentation,
        # multicast, unspecified, and other special-use ranges.
        if not address.is_global:
            return None
        if str(address) not in addresses:
            addresses.append(str(address))
    return addresses or None


async def _dns_fetch_allowed(url: str, settings: Settings) -> bool:
    return await _validated_addresses(url, settings) is not None


def _pinned_url(url: str, address: str) -> str:
    parsed = urlparse(url)
    host = f"[{address}]" if ":" in address else address
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return urlunparse(parsed._replace(netloc=netloc))


def _host_header(parsed) -> str:
    host = parsed.hostname or ""
    if ":" in host:  # HTTP Host syntax brackets an IPv6 literal.
        host = f"[{host}]"
    return f"{host}:{parsed.port}" if parsed.port else host


@asynccontextmanager
async def _safe_stream(client: httpx.AsyncClient, method: str, url: str, settings: Settings):
    """Fetch external HTTP only through a DNS-pinned connection.

    HTTPS requests retain the original Host header and pass its hostname as
    HTTP Core's SNI override, so certificate validation remains for the name
    the user supplied. Redirects are explicit and every target is validated
    and pinned independently.
    """
    current = url
    response: httpx.Response | None = None
    for hop in range(settings.redirect_max_hops + 1):
        addresses = await _validated_addresses(current, settings)
        if addresses is None:
            raise ValueError("refusing to fetch internal/private URL")
        parsed = urlparse(current)
        if addresses:
            request_url = _pinned_url(current, addresses[0])
            # A pool keyed by numeric address could otherwise reuse a TLS
            # connection verified for a different hostname sharing that IP.
            # Keep Host/SNI for the original name and isolate each request.
            headers = {"host": _host_header(parsed), "connection": "close"}
            extensions = {"sni_hostname": parsed.hostname}
        else:
            request_url, headers, extensions = current, {}, {}
        request = client.build_request(method, request_url, headers=headers, extensions=extensions)
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


def _fetch_allowed(url: str, settings: Settings) -> bool:
    """The resolver's own gateways are always fetchable; anything else must
    pass the external-URL check. A URL only counts as a gateway URL when it
    is exactly the base or a path under it — a bare prefix test would let
    look-alike ports through (e.g. 127.0.0.1:30001 vs the :3000 gateway)."""
    for base in (
        settings.ipfs_internal, settings.arweave_internal,
        settings.arweave_retained_internal,
    ):
        base = base.rstrip("/")
        if url == base or url.startswith(base + "/"):
            return True
    return external_url_ok(url)


async def _bounded_text(client: httpx.AsyncClient, url: str, max_bytes: int, settings: Settings) -> str:
    """GET a text body, refusing to buffer more than `max_bytes`."""
    async with _safe_stream(client, "GET", url, settings) as response:
        response.raise_for_status()
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise ValueError(f"response larger than {max_bytes} bytes")
        size = 0
        chunks: list[bytes] = []
        async for chunk in response.aiter_bytes(65536):
            size += len(chunk)
            if size > max_bytes:
                raise ValueError(f"response larger than {max_bytes} bytes")
            chunks.append(chunk)
        encoding = response.encoding or "utf-8"
    return b"".join(chunks).decode(encoding, errors="replace")


def _internal_fetch_url(ref: str, settings: Settings) -> str:
    """Where the resolver itself fetches `ref` from: the on-box gateways."""
    ipfs = ipfs_parts(ref)
    if ipfs is not None:
        cid, path = ipfs
        return f"{settings.ipfs_internal}/ipfs/{cid}{path}"
    arweave = arweave_parts(ref)
    if arweave is not None:
        txid, path = arweave
        base = (
            settings.arweave_retained_internal
            if retained_state(txid, path, settings) == "kept"
            else settings.arweave_internal
        )
        return f"{base.rstrip('/')}/{txid}{path}"
    return ref


def _main_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    return content_type.split(";", 1)[0].strip().lower()


def _decode_embedded_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return html.unescape(value.replace("\\/", "/").replace("\\u0026", "&"))


def _extract_embedded_value(text: str, key: str) -> str | None:
    match = re.search(_EMBEDDED_STRING_RE.format(key=re.escape(key)), text)
    if not match:
        return None
    return _decode_embedded_string(match.group(1))


def _to_verse_source_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.hostname not in _VERSE_HOSTS:
        return url

    prefix = "/image/"
    if not parsed.path.startswith(prefix):
        return url

    remainder = parsed.path[len(prefix) :]
    parts = remainder.split("/", 1)
    if len(parts) != 2:
        return url

    encoded_path = parts[1].split("@", 1)[0]
    return f"https://verse.works/image/source/{encoded_path}"


async def resolve_ref(
    ref: str,
    settings: Settings,
    client: httpx.AsyncClient,
    _depth: int = 0,
    origin: str | None = None,
) -> Resolved:
    """Resolve a reference to a box-local playable URL.

    Network-dependent steps (probes, metadata fetch, verse scrape) degrade
    gracefully: a failure returns the best mechanical answer with a note
    rather than raising.
    """
    ref = ref.strip()
    if origin:
        # Recursive resolver calls inherit this request origin through Settings,
        # even where legacy helpers do not take an origin argument.
        settings = settings.model_copy(update={
            "ipfs_public_base": origin.rstrip("/"),
            "arweave_public_base": origin.rstrip("/"),
        })
    if _depth > _MAX_DEPTH:
        return Resolved(ref, ref, "play", None, False, note="recursion limit reached")

    if settings.overrides_path:
        override = get_registry(settings.overrides_path).lookup(ref)
        if override is not None:
            # Checked at every recursion depth on purpose: a dead ref is
            # usually discovered inside live metadata, not at the entry point.
            inner = await resolve_ref(override.replacement, settings, client, _depth + 1)
            return replace(
                inner,
                original_ref=ref,
                substituted=True,
                substituted_ref=ref,
                substitution_status=override.status,
            )

    ipfs = ipfs_parts(ref)
    if ipfs is not None:
        cid, path = ipfs
        result = await _resolve_ipfs(ref, cid, path, settings, client, _depth, origin)
        result.source_kind = result.source_kind or "ipfs"
        return result

    arweave = arweave_parts(ref)
    if arweave is not None:
        txid, path = arweave
        result = await _resolve_arweave(ref, txid, path, settings, client, _depth, origin)
        result.source_kind = result.source_kind or "arweave"
        return result

    if ref.startswith("data:"):
        return await _resolve_data_uri(ref, settings, client, _depth, origin)

    parsed = urlparse(ref)
    if parsed.hostname in _VERSE_HOSTS and parsed.path.startswith("/artworks/"):
        return await _resolve_verse(ref, settings, client, _depth, origin)

    if parsed.scheme in {"http", "https"}:
        return await _resolve_direct(ref, parsed.path, settings, client, _depth, origin)

    return Resolved(ref, ref, "play", None, False, note="unrecognized reference")


async def _resolve_ipfs(
    ref: str, cid: str, path: str, settings: Settings, client, depth: int, origin: str | None
) -> Resolved:
    query = urlparse(ref).query
    public = f"{origin.rstrip('/') if origin else settings.ipfs_public_base}/ipfs/{cid}{path}"
    internal = f"{settings.ipfs_internal}/ipfs/{cid}{path}"
    native_ref = f"ipfs://{cid}{path}"
    if query:
        public = f"{public}?{query}"

    # Even a familiar suffix is only a rendering hint.  A successful resolve
    # means Kubo can serve the requested artifact now.
    headers = await probe_headers(client, internal)
    if headers is None:
        return Resolved(
            ref, public, "play", "ipfs", False, source_kind="ipfs", final_ref=native_ref,
            note="local IPFS backend cannot serve this artifact",
        )
    seg = (path or cid).rsplit("/", 1)[-1]
    ext = extension_of(seg)
    if ext == "json":
        return await _resolve_token_metadata(
            ref, internal, "ipfs", settings, client, depth,
            source_kind="ipfs", final_ref=native_ref,
        )

    content_type = headers.get("content-type")
    main = _main_content_type(content_type)

    if main == "application/json":
        return await _resolve_token_metadata(
            ref, internal, "ipfs", settings, client, depth,
            source_kind="ipfs", final_ref=native_ref,
        )
    if headers is not None and main is None:
        # Kubo answers a directory HEAD with 200 and no Content-Type (files
        # always carry one): list the directory and descend.
        descended = await _resolve_ipfs_dir(ref, cid, path, settings, client, depth)
        if descended is not None:
            return descended
    if main == "text/html":
        return Resolved(
            ref, public, "send", "ipfs", True, content_type=content_type,
            source_kind="ipfs", final_ref=native_ref, keep_state="live-dependent",
            integrity={"algorithm": "ipfs-cid", "digest": cid},
        )

    hinted_ext = ext_from_content_type(content_type)
    if hinted_ext and not query and ext not in KNOWN_EXTENSIONS:
        public = f"{public}?filename=art.{hinted_ext}"
    method = infer_playback_method(path or cid)
    return Resolved(
        ref, public, method, "ipfs", True, content_type=content_type,
        source_kind="ipfs", final_ref=native_ref,
        keep_state="live-dependent" if method == "send" else "cached",
        # A CID names an IPLD graph, not necessarily one flat byte stream.
        integrity={"algorithm": "ipfs-cid", "digest": cid},
    )


async def _resolve_ipfs_dir(
    ref: str, cid: str, path: str, settings: Settings, client, depth: int
) -> Resolved | None:
    """Descend into a UnixFS directory: pick its largest file child and recurse.

    A bare directory CID otherwise reaches renderers as the gateway's listing
    page — the original iframe bug. Uses Kubo's machine-readable `ls` (the
    seeding surface already requires the API), whose links carry sizes, so no
    per-child probing is needed. Returns None when the listing can't be
    fetched or looks like more than a media wrapper; the caller falls back to
    the plain gateway URL.
    """
    try:
        response = await client.post(
            f"{settings.ipfs_api}/api/v0/ls",
            params={"arg": f"/ipfs/{cid}{path}"},
        )
        response.raise_for_status()
        objects = response.json().get("Objects") or []
        links = (objects[0].get("Links") or []) if objects else []
    except (httpx.HTTPError, ValueError, AttributeError, IndexError, KeyError):
        return None

    files = [link for link in links if link.get("Type") == 2 and link.get("Name")]
    if not files or len(files) > _MAX_DIR_CHILDREN:
        return None

    largest = max(files, key=lambda link: link.get("Size") or 0)
    target = f"ipfs://{cid}{path.rstrip('/')}/{quote(str(largest['Name']))}"
    inner = await resolve_ref(target, settings, client, depth + 1)
    return replace(inner, original_ref=ref)


async def _resolve_arweave(
    ref: str, txid: str, path: str, settings: Settings, client, depth: int, origin: str | None
) -> Resolved:
    # The path is part of the identity: Arweave path manifests resolve
    # txid/sub/path to a distinct resource (e.g. per-token metadata files
    # beneath one manifest txid).
    query = urlparse(ref).query
    public = f"{origin.rstrip('/') if origin else settings.arweave_public_base}/arweave/{txid}{path}"
    native_ref = f"ar://{txid}{path}"
    if query:
        public = f"{public}?{query}"

    # A registry-kept identity is exclusively served from the retained Core.
    # Do this before every probe and metadata fetch: ordinary Envoy bytes must
    # never mask a missing retained artifact.
    state = retained_state(txid, path, settings)
    if state == "kept" and not await retained_available(txid, path, settings, client):
        return Resolved(
            ref, public, "play", "arweave", False, source_kind="arweave", final_ref=native_ref,
            keep_state="degraded",
            note="retained AR.IO plane is unavailable; not falling back for kept identity",
        )
    retained = state == "kept"
    base = settings.arweave_retained_internal if retained else settings.arweave_internal
    internal = f"{base.rstrip('/')}/{txid}{path}"
    headers = await probe_headers(client, internal)
    if headers is None or (retained and headers.get("x-cache", "").strip().lower() != "hit"):
        return Resolved(
            ref, public, "play", "arweave", False, source_kind="arweave", final_ref=native_ref,
            keep_state="degraded" if retained else "cached",
            note=("retained AR.IO plane is unavailable; not falling back for kept identity"
                  if retained else "local AR.IO backend cannot serve this artifact"),
        )
    content_type = headers.get("content-type")
    main = _main_content_type(content_type)

    integrity = _arweave_integrity(headers)
    if main == "application/json":
        # The metadata identity may be retained while its final media is not;
        # recursion reports the final artifact's own keep state.
        return await _resolve_token_metadata(
            ref, internal, "arweave", settings, client, depth,
            source_kind="arweave", final_ref=native_ref,
        )
    method = "send" if main == "text/html" else "play"
    return Resolved(
        ref, public, method, "arweave", True, content_type=content_type,
        source_kind="arweave", final_ref=native_ref,
        keep_state=("live-dependent" if main == "text/html" else "kept" if retained else "cached"),
        integrity=integrity,
    )


async def _resolve_direct(
    ref: str, path: str, settings: Settings, client, depth: int, origin: str | None
) -> Resolved:
    # Bound concurrent external body streams; metadata-only recursive work is
    # small and separately bounded by fetch_max_bytes.
    async with _static_fetch_slot(settings):
        return await _resolve_direct_fetch(ref, path, settings, client, depth, origin)


async def _resolve_direct_fetch(
    ref: str, path: str, settings: Settings, client, depth: int, origin: str | None
) -> Resolved:
    seg = path.rsplit("/", 1)[-1]
    ext = extension_of(seg)
    if not await _dns_fetch_allowed(ref, settings):
        # Everything past here fetches/probes the URL — resolve DNS first and
        # refuse internal/private answers before any connection.
        return Resolved(ref, ref, "play", None, False, note="refusing to fetch internal/private URL")
    if ext == "json":
        return await _resolve_token_metadata(ref, ref, "token-metadata", settings, client, depth)

    # HTTP is copied to Curio's static backend; it is never added to Kubo.
    # Stream it to a bounded tempfile rather than collecting an attacker-sized
    # body in memory. _safe_stream pins DNS and validates every redirect hop.
    store = StaticStore(settings.static_root)
    temporary: str | None = None
    try:
        async with _safe_stream(client, "GET", ref, settings) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type")
            declared = response.headers.get("content-length")
            cap = settings.fetch_max_bytes if _main_content_type(content_type) == "application/json" else settings.static_max_bytes
            if declared and declared.isdigit() and int(declared) > cap:
                raise ValueError(f"response larger than {cap} bytes")
            if _main_content_type(content_type) == "application/json":
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes(65536):
                    size += len(chunk)
                    if size > cap:
                        raise ValueError(f"response larger than {cap} bytes")
                    chunks.append(chunk)
                return await _resolve_metadata_dict(ref, json.loads(b"".join(chunks)), "token-metadata", settings, client, depth)
            store.root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=store.root, prefix=".fetch-", delete=False) as output:
                temporary = output.name
                size = 0
                async for chunk in response.aiter_bytes(65536):
                    size += len(chunk)
                    if size > cap:
                        raise ValueError(f"response larger than {cap} bytes")
                    output.write(chunk)
        entry = store.put_file(Path(temporary), media_type=content_type,
                               filename=seg or None, source_ref=ref)
        temporary = None  # put_file atomically moved it
    except (httpx.HTTPError, ValueError, OSError, json.JSONDecodeError) as exc:
        return Resolved(
            ref, ref, "play", "http", False, note=f"media fetch failed: {exc}",
            source_kind="http", final_ref=ref,
        )
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    public_origin = origin.rstrip("/") if origin else settings.ipfs_public_base.rstrip("/")
    url = f"{public_origin}/media/{entry['id']}"
    method = "send" if _main_content_type(content_type) == "text/html" else infer_playback_method(path)
    return Resolved(ref, url, method, "http", True, content_type=content_type, source_kind="http", final_ref=ref,
        keep_state="live-dependent" if _main_content_type(content_type) == "text/html" else "cached",
        integrity={"algorithm": "sha256", "digest": str(entry["digest"])})


def _arweave_integrity(headers: httpx.Headers) -> dict[str, str] | None:
    """The AR.IO data digest, when its successful response exposes one."""
    digest = headers.get("content-digest") or headers.get("x-ar-io-digest")
    return {"algorithm": "arweave-data-digest", "digest": digest} if digest else None


async def _resolve_token_metadata(
    ref: str,
    fetch_url: str,
    provider: str,
    settings: Settings,
    client: httpx.AsyncClient,
    depth: int,
    *,
    source_kind: str | None = None,
    final_ref: str | None = None,
) -> Resolved:
    """Resolve metadata while retaining a recognized native identity on failure."""
    failure = {"source_kind": source_kind, "final_ref": final_ref}
    if not await _dns_fetch_allowed(fetch_url, settings):
        return Resolved(
            ref, ref, "play", provider, False,
            note="refusing to fetch internal/private URL", **failure,
        )
    try:
        metadata: Any = json.loads(await _bounded_text(client, fetch_url, settings.fetch_max_bytes, settings))
    except (httpx.HTTPError, ValueError) as exc:
        return Resolved(
            ref, ref, "play", provider, False,
            note=f"metadata fetch failed: {exc}", **failure,
        )
    if not isinstance(metadata, dict):
        return Resolved(
            ref, ref, "play", provider, False, note="metadata is not a JSON object", **failure,
        )
    return await _resolve_metadata_dict(ref, metadata, provider, settings, client, depth)


async def _resolve_metadata_dict(
    ref: str, metadata: dict[str, Any], provider: str, settings: Settings, client, depth: int
) -> Resolved:
    name = metadata.get("name")
    title = name if isinstance(name, str) else None

    target = pick_media_field(metadata) or await _pick_image(metadata, settings, client)
    if target is None:
        return Resolved(
            ref, ref, "play", provider, False, title=title,
            note="metadata has no animation/image field",
        )

    inner = await resolve_ref(target, settings, client, depth + 1)
    return replace(inner, original_ref=ref, provider=provider, title=title or inner.title)


_DATA_URI_RE = re.compile(r"^data:([^,]*),(.*)$", re.DOTALL)


async def _resolve_data_uri(
    ref: str, settings: Settings, client, depth: int, origin: str | None
) -> Resolved:
    """RFC 2397 data: URIs — the tokenURI form of fully on-chain works.

    JSON metadata is decoded and recursed into like any other tokenURI;
    anything else (inline SVG, HTML, images) is already playable bytes and
    passes through — a data: URL is its own self-contained content.
    """
    match = _DATA_URI_RE.match(ref)
    if match is None:
        return Resolved(ref, ref, "play", "data", False, note="malformed data: URI", final_ref=ref)
    params, payload = match.group(1).split(";"), match.group(2)
    mediatype = params[0].strip().lower() or "text/plain"
    is_base64 = any(p.strip().lower() == "base64" for p in params[1:])
    # Reject before decode: base64 expands by at most 3/4, percent encoding
    # can only shrink. This prevents a giant tokenURI from allocating first.
    encoded_limit = settings.data_max_bytes * (4 // 3 + 1) + 16
    if len(payload) > encoded_limit:
        return Resolved(ref, ref, "play", "data", False, note="data: URI exceeds configured size limit", final_ref=ref)

    if mediatype == "application/json":
        try:
            text = (
                base64.b64decode(payload, validate=False).decode("utf-8", errors="replace")
                if is_base64
                else unquote(payload)
            )
            if len(text.encode("utf-8")) > settings.data_max_bytes:
                raise ValueError("data: URI exceeds configured size limit")
            metadata: Any = json.loads(text)
        except ValueError as exc:
            return Resolved(ref, ref, "play", "data", False, note=f"data: URI decode failed: {exc}", final_ref=ref)
        if not isinstance(metadata, dict):
            return Resolved(ref, ref, "play", "data", False, note="metadata is not a JSON object", final_ref=ref)
        return await _resolve_metadata_dict(ref, metadata, "data", settings, client, depth)

    try:
        data = base64.b64decode(payload, validate=True) if is_base64 else unquote(payload).encode()
    except ValueError as exc:
        return Resolved(ref, ref, "play", "data", False, note=f"data: URI decode failed: {exc}", final_ref=ref)
    if len(data) > settings.data_max_bytes:
        return Resolved(ref, ref, "play", "data", False, note="data: URI exceeds configured size limit", final_ref=ref)
    entry = StaticStore(settings.static_root).put(data, media_type=mediatype, filename=None, source_ref=ref)
    base = origin.rstrip("/") if origin else settings.ipfs_public_base.rstrip("/")
    method = "send" if mediatype == "text/html" else "play"
    return Resolved(ref, f"{base}/media/{entry['id']}", method, "data", True, content_type=mediatype,
        source_kind="data", final_ref=ref, keep_state="live-dependent" if mediatype == "text/html" else "cached",
        integrity={"algorithm": "sha256", "digest": str(entry["digest"])})


def pick_media_field(metadata: dict[str, Any]) -> str | None:
    for key in _ANIMATION_FIELDS:
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None


async def _pick_image(metadata: dict[str, Any], settings: Settings, client) -> str | None:
    candidates: list[str] = []
    for key in _IMAGE_FIELDS:
        value = metadata.get(key)
        if isinstance(value, str) and value and value not in candidates:
            if value.startswith(("http://", "https://")) and not await _dns_fetch_allowed(value, settings):
                continue  # metadata-supplied URL pointing somewhere internal
            candidates.append(value)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    async def size(candidate: str) -> int:
        try:
            async with _safe_stream(client, "HEAD", _internal_fetch_url(candidate, settings), settings) as response:
                response.raise_for_status()
                return int(response.headers.get("content-length", "-1"))
        except (httpx.HTTPError, ValueError):
            return -1
    sizes = await asyncio.gather(*(size(candidate) for candidate in candidates))
    best = max(range(len(candidates)), key=lambda i: sizes[i])
    return candidates[best] if sizes[best] >= 0 else candidates[0]


def _verse_scrape_urls(ref: str) -> list[str]:
    """The page as given, plus the base artwork page when `ref` is an edition
    sub-page (/artworks/<id>/<edition>) — edition pages carry only the
    site-generic og:image while the base page has the artwork's."""
    parsed = urlparse(ref)
    parts = parsed.path.split("/")
    urls = [ref]
    if len(parts) > 3:
        urls.append(urlunparse(parsed._replace(path="/".join(parts[:3]), query="", fragment="")))
    return urls


async def _resolve_verse(
    ref: str, settings: Settings, client, depth: int, origin: str | None
) -> Resolved:
    note = "no tokenUri/iframeUrl/og:image found in verse page"
    for page_url in _verse_scrape_urls(ref):
        try:
            text = await _bounded_text(client, page_url, settings.fetch_max_bytes, settings)
        except (httpx.HTTPError, ValueError) as exc:
            note = f"verse fetch failed: {exc}"
            continue

        token_uri = _extract_embedded_value(text, "tokenUri")
        if token_uri:
            ipfs = ipfs_parts(token_uri)
            arweave = arweave_parts(token_uri)
            source_kind = "ipfs" if ipfs is not None else "arweave" if arweave is not None else None
            final_ref = (
                f"ipfs://{ipfs[0]}{ipfs[1]}" if ipfs is not None
                else f"ar://{arweave[0]}{arweave[1]}" if arweave is not None else None
            )
            return await _resolve_token_metadata(
                ref, _internal_fetch_url(token_uri, settings), "verse", settings, client, depth,
                source_kind=source_kind, final_ref=final_ref,
            )

        iframe_url = _extract_embedded_value(text, "iframeUrl")
        if iframe_url:
            inner = await resolve_ref(iframe_url, settings, client, depth + 1)
            return replace(inner, original_ref=ref, provider="verse", playback_method="send")

        for match in _META_IMAGE_RE.finditer(text):
            image_url = html.unescape(match.group(1))
            if urlparse(image_url).path == "/opengraph-image.png":
                continue  # verse's site-wide default, not the artwork
            inner = await resolve_ref(_to_verse_source_url(image_url), settings, client, depth + 1)
            return replace(inner, original_ref=ref, provider="verse")

    return Resolved(ref, ref, "send", "verse", False, note=note)

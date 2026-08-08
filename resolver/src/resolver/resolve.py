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

import base64
import html
import ipaddress
import json
import re
from dataclasses import asdict, dataclass, replace
from typing import Any
from urllib.parse import quote, unquote, urlparse, urlunparse

import httpx

from .config import Settings
from .fixups import (
    KNOWN_EXTENSIONS,
    ext_from_content_type,
    extension_of,
    infer_playback_method,
    pick_largest,
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
    keep_state: str = "cached"
    integrity: dict[str, str] | None = None

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        # media_url is the product contract; resolved_url remains temporarily
        # for existing API consumers.
        payload["media_url"] = self.resolved_url
        payload["media_type"] = self.content_type
        return payload


def external_url_ok(url: str) -> bool:
    """Refuse obviously-internal destinations in user/metadata-supplied URLs.

    Literal-IP and localhost checks only — hostname-based private targets and
    redirect chains are accepted under the LAN trust model (docs/design.md).
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
    return not (
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_multicast or addr.is_reserved or addr.is_unspecified
    )


def _fetch_allowed(url: str, settings: Settings) -> bool:
    """The resolver's own gateways are always fetchable; anything else must
    pass the external-URL check. A URL only counts as a gateway URL when it
    is exactly the base or a path under it — a bare prefix test would let
    look-alike ports through (e.g. 127.0.0.1:30001 vs the :3000 gateway)."""
    for base in (settings.ipfs_internal, settings.arweave_internal):
        base = base.rstrip("/")
        if url == base or url.startswith(base + "/"):
            return True
    return external_url_ok(url)


async def _bounded_text(client: httpx.AsyncClient, url: str, max_bytes: int) -> str:
    """GET a text body, refusing to buffer more than `max_bytes`."""
    async with client.stream("GET", url) as response:
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
        return f"{settings.arweave_internal}/{txid}{path}"
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
    if query:
        public = f"{public}?{query}"

    seg = (path or cid).rsplit("/", 1)[-1]
    ext = extension_of(seg)
    if ext == "json":
        return await _resolve_token_metadata(ref, internal, "ipfs", settings, client, depth)

    if query or ext in KNOWN_EXTENSIONS:
        # A known media/HTML extension (or an existing query, e.g. ?filename=)
        # already satisfies the FF1's extension sniff — no probe needed.
        # Unknown extensions fall through to the probe.
        return Resolved(ref, public, infer_playback_method(path or cid), "ipfs", True)

    headers = await probe_headers(client, internal)
    content_type = headers.get("content-type") if headers is not None else None
    main = _main_content_type(content_type)

    if main == "application/json":
        return await _resolve_token_metadata(ref, internal, "ipfs", settings, client, depth)
    if headers is not None and main is None:
        # Kubo answers a directory HEAD with 200 and no Content-Type (files
        # always carry one): list the directory and descend.
        descended = await _resolve_ipfs_dir(ref, cid, path, settings, client, depth)
        if descended is not None:
            return descended
    if main == "text/html":
        return Resolved(ref, public, "send", "ipfs", True, content_type=content_type)

    ext = ext_from_content_type(content_type)
    if ext:
        public = f"{public}?filename=art.{ext}"
    note = None if headers is not None else "gateway probe failed; no filename hint"
    return Resolved(
        ref, public, infer_playback_method(path or cid), "ipfs", True,
        content_type=content_type, note=note,
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
    internal = f"{settings.arweave_internal}/{txid}{path}"
    if query:
        public = f"{public}?{query}"

    headers = await probe_headers(client, internal)
    content_type = headers.get("content-type") if headers is not None else None
    main = _main_content_type(content_type)

    if main == "application/json":
        return await _resolve_token_metadata(ref, internal, "arweave", settings, client, depth)
    method = "send" if main == "text/html" else "play"
    return Resolved(ref, public, method, "arweave", True, content_type=content_type)


async def _resolve_direct(
    ref: str, path: str, settings: Settings, client, depth: int, origin: str | None
) -> Resolved:
    seg = path.rsplit("/", 1)[-1]
    ext = extension_of(seg)
    if not external_url_ok(ref):
        # Everything past here fetches/probes the URL — refuse internal targets.
        return Resolved(ref, ref, "play", None, False, note="refusing to fetch internal/private URL")
    if ext == "json":
        return await _resolve_token_metadata(ref, ref, "token-metadata", settings, client, depth)

    # HTTP is copied to Curio's static backend; it is never added to Kubo.
    try:
        async with client.stream("GET", ref, follow_redirects=False) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type")
            if _main_content_type(content_type) == "application/json":
                text = await _bounded_text(client, ref, settings.fetch_max_bytes)
                return await _resolve_metadata_dict(ref, json.loads(text), "token-metadata", settings, client, depth)
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes(65536):
                size += len(chunk)
                if size > settings.static_max_bytes:
                    raise ValueError(f"response larger than {settings.static_max_bytes} bytes")
                chunks.append(chunk)
    except (httpx.HTTPError, ValueError) as exc:
        return Resolved(ref, ref, "play", "http", False, note=f"media fetch failed: {exc}", source_kind="http")
    entry = StaticStore(settings.static_root).put(b"".join(chunks), media_type=content_type,
        filename=seg or None, source_ref=ref)
    public_origin = origin.rstrip("/") if origin else settings.ipfs_public_base.rstrip("/")
    url = f"{public_origin}/media/{entry['id']}"
    method = "send" if _main_content_type(content_type) == "text/html" else infer_playback_method(path)
    return Resolved(ref, url, method, "http", True, content_type=content_type, source_kind="http",
        integrity={"algorithm": "sha256", "digest": str(entry["digest"])})


async def _resolve_token_metadata(
    ref: str, fetch_url: str, provider: str, settings: Settings, client, depth: int
) -> Resolved:
    if not _fetch_allowed(fetch_url, settings):
        return Resolved(ref, ref, "play", provider, False, note="refusing to fetch internal/private URL")
    try:
        metadata: Any = json.loads(await _bounded_text(client, fetch_url, settings.fetch_max_bytes))
    except (httpx.HTTPError, ValueError) as exc:
        return Resolved(ref, ref, "play", provider, False, note=f"metadata fetch failed: {exc}")
    if not isinstance(metadata, dict):
        return Resolved(ref, ref, "play", provider, False, note="metadata is not a JSON object")
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
        return Resolved(ref, ref, "play", "data", False, note="malformed data: URI")
    params, payload = match.group(1).split(";"), match.group(2)
    mediatype = params[0].strip().lower() or "text/plain"
    is_base64 = any(p.strip().lower() == "base64" for p in params[1:])

    if mediatype == "application/json":
        try:
            text = (
                base64.b64decode(payload, validate=False).decode("utf-8", errors="replace")
                if is_base64
                else unquote(payload)
            )
            metadata: Any = json.loads(text)
        except ValueError as exc:
            return Resolved(ref, ref, "play", "data", False, note=f"data: URI decode failed: {exc}")
        if not isinstance(metadata, dict):
            return Resolved(ref, ref, "play", "data", False, note="metadata is not a JSON object")
        return await _resolve_metadata_dict(ref, metadata, "data", settings, client, depth)

    try:
        data = base64.b64decode(payload, validate=True) if is_base64 else unquote(payload).encode()
    except ValueError as exc:
        return Resolved(ref, ref, "play", "data", False, note=f"data: URI decode failed: {exc}")
    entry = StaticStore(settings.static_root).put(data, media_type=mediatype, filename=None, source_ref=ref)
    base = origin.rstrip("/") if origin else settings.ipfs_public_base.rstrip("/")
    method = "send" if mediatype == "text/html" else "play"
    return Resolved(ref, f"{base}/media/{entry['id']}", method, "data", True, content_type=mediatype,
        source_kind="data", integrity={"algorithm": "sha256", "digest": str(entry["digest"])})


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
            if value.startswith(("http://", "https://")) and not external_url_ok(value):
                continue  # metadata-supplied URL pointing somewhere internal
            candidates.append(value)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return await pick_largest(
        [(c, _internal_fetch_url(c, settings)) for c in candidates], client
    )


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
            text = await _bounded_text(client, page_url, settings.fetch_max_bytes)
        except (httpx.HTTPError, ValueError) as exc:
            note = f"verse fetch failed: {exc}"
            continue

        token_uri = _extract_embedded_value(text, "tokenUri")
        if token_uri:
            return await _resolve_token_metadata(
                ref, _internal_fetch_url(token_uri, settings), "verse", settings, client, depth
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

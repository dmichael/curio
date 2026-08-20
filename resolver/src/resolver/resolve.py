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
  - verse.works/artworks/... and verse.works/items/ethereum/<contract>/<id>
    -> chain-first: get the contract address + token id (scraped from the
    artwork page, or already in the /items/ URL itself), call ERC-721
    tokenURI (or ERC-1155 uri) over the configured RPC, and resolve that like
    any other tokenURI; only when chain resolution is impossible (no
    coordinates, RPC disabled/failed, metadata unreachable) fall back to
    scraping a page directly (tokenUri / iframeUrl / og:image)

Every step first consults the operator's override registry (overrides.py):
a ref whose canonical content is gone resolves to its recorded replacement,
marked `substituted` with a provenance status — never silently.

Not built: ENS / wallet / tx resolution generally (needs an RPC/indexer path
chosen for the service; see docs/design.md § Open decisions). Contract+tokenId
resolution is built, but only for Verse references (see `_resolve_verse` and
`_resolve_verse_items`).
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import tempfile
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse, urlunparse

import httpx

from . import safe_fetch
from .config import Settings
from .fixups import (
    KNOWN_EXTENSIONS,
    ext_from_content_type,
    extension_of,
    infer_playback_method,
    probe_headers,
)
from .html_audit import external_markup_refs
from .overrides import get_registry
from .refs import arweave_parts, ipfs_parts
from .static_store import CacheQuotaError, ResolutionStatus, StaticStore, playable

__all__ = [
    "Resolved",
    "resolve_ref",
    "storage_intent",
    "pick_media_field",
    "external_url_ok",
]

_VERSE_HOSTS = {"verse.works", "www.verse.works"}
_META_IMAGE_RE = re.compile(
    r'<meta\s+(?:property|name)="(?:og:image|twitter:image)"\s+content="([^"]+)"',
    re.IGNORECASE,
)
_EMBEDDED_STRING_RE = r'\\"{key}\\":\\"([^\\"]+)\\"'
# Verse's embedded page JSON carries a contract address under this exact key
# for tokenized artworks, but never a numeric token id under any name we've
# observed (checked both sold and unsold artwork pages) — an unsold,
# not-yet-minted piece has a contract but genuinely no on-chain token yet.
# These candidate key names cover platforms/collections that do publish one,
# either as a JSON string or a bare number.
_CONTRACT_ADDRESS_RE = re.compile(r'\\"contractAddress\\":\\"(0x[0-9a-fA-F]{40})\\"')
_TOKEN_ID_RE = re.compile(r'\\"(?:tokenId|tokenID|token_id)\\":(?:\\")?(\d+)(?:\\")?')
# /items/<chain>/<contract>/<tokenId> names its chain coordinates directly in
# the URL — no page scrape needed to find them. Only "ethereum" is supported
# today; the address and token id are validated on top of the path shape.
_ITEMS_PATH_RE = re.compile(r"^/items/([^/]+)/([^/]+)/([^/]+)$")
_HEX_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

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
_STORAGE_INTENT: ContextVar[bool] = ContextVar("storage_intent", default=False)


@contextmanager
def storage_intent():
    """Make final static artifacts non-evictable throughout recursive resolution."""
    token = _STORAGE_INTENT.set(True)
    try:
        yield
    finally:
        _STORAGE_INTENT.reset(token)


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
    status: ResolutionStatus = ResolutionStatus.READY
    integrity: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if not self.resolved:
            self.status = ResolutionStatus.FAILED

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["source_ref"] = self.final_ref
        # media_url is the product contract; resolved_url remains temporarily
        # for existing API consumers.
        payload["media_url"] = self.resolved_url
        payload["media_type"] = self.content_type
        return payload


def external_url_ok(url: str) -> bool:
    """Refuse obviously-internal destinations in user/metadata-supplied URLs."""
    return safe_fetch.external_url_ok(url)


async def _validated_addresses(url: str, settings: Settings) -> list[str] | None:
    return await safe_fetch.validated_addresses(url, settings)


async def _dns_fetch_allowed(url: str, settings: Settings) -> bool:
    return await _validated_addresses(url, settings) is not None


@asynccontextmanager
async def _safe_stream(
    client: httpx.AsyncClient, method: str, url: str, settings: Settings, *, timeout: float | None = None
):
    """Compatibility wrapper around the shared pinned untrusted stream."""
    async with safe_fetch.safe_stream(client, method, url, settings, timeout=timeout) as response:
        yield response


def _fetch_allowed(url: str, settings: Settings) -> bool:
    return safe_fetch.fetch_allowed(url, settings)


async def _bounded_text(client: httpx.AsyncClient, url: str, max_bytes: int, settings: Settings) -> str:
    """GET a text body, refusing to buffer more than `max_bytes`."""
    arweave_internal = url.startswith(settings.arweave_internal.rstrip("/") + "/")
    timeout = settings.arweave_cold_timeout if arweave_internal else None
    async with _safe_stream(client, "GET", url, settings, timeout=timeout) as response:
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


async def _audit_runtime_html(
    internal: str, settings: Settings, client
) -> tuple[ResolutionStatus, str | None]:
    """Classify a stored HTML artifact by its declared subresources.

    Markup whose references are all relative lives entirely inside the pinned
    CID graph or Arweave manifest, so the artifact is `ready`. Any absolute
    reference — or a body the audit cannot read — keeps the conservative
    `live-dependent` answer. Markup-level only: a URL a script assembles at
    runtime is invisible here (see html_audit).
    """
    try:
        text = await _bounded_text(client, internal, settings.fetch_max_bytes, settings)
    except (httpx.HTTPError, ValueError):
        return ResolutionStatus.LIVE_DEPENDENT, None
    external = external_markup_refs(text)
    if external:
        shown = ", ".join(external[:3])
        return ResolutionStatus.LIVE_DEPENDENT, f"external references: {shown}"
    return ResolutionStatus.READY, None


def _internal_fetch_url(ref: str, settings: Settings) -> str:
    """Where the resolver itself fetches `ref` from: the on-box gateways."""
    ipfs = ipfs_parts(ref)
    if ipfs is not None:
        cid, path = ipfs
        return f"{settings.ipfs_internal}/ipfs/{cid}{path}"
    arweave = arweave_parts(ref)
    if arweave is not None:
        txid, path = arweave
        return f"{settings.arweave_internal.rstrip('/')}/{txid}{path}"
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

    if ref.startswith(("upload:sha256:", "data:sha256:")):
        record = StaticStore(
            settings.static_root, settings.static_cache_max_bytes
        ).resolution(ref)
        if record is None:
            return Resolved(ref, ref, "play", None, False, note="stored reference not found")
        if not playable(record):
            # Mirror GET /resolve: a recorded failure resolves to nothing,
            # never to the media path the record says did not work.
            return Resolved(
                ref,
                ref,
                "play",
                None,
                False,
                status=ResolutionStatus.FAILED,
                note=str(record.get("reason") or "recorded failure"),
            )
        base = origin.rstrip("/") if origin else settings.ipfs_public_base.rstrip("/")
        media_type = record.get("media_type")
        status = ResolutionStatus(str(record["status"]))
        source_kind = "upload" if ref.startswith("upload:") else "data"
        digest = ref.rsplit(":", 1)[-1]
        return Resolved(
            ref,
            f"{base}{record['media_path']}",
            "send" if status == ResolutionStatus.LIVE_DEPENDENT else "play",
            source_kind,
            True,
            content_type=str(media_type) if media_type else None,
            source_kind=source_kind,
            final_ref=str(record["final_ref"]),
            status=status,
            integrity={"algorithm": "sha256", "digest": digest},
        )

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
    if parsed.hostname in _VERSE_HOSTS:
        if parsed.path.startswith("/artworks/"):
            return await _resolve_verse(ref, settings, client, _depth, origin)
        items_coordinates = _verse_items_coordinates(parsed.path)
        if items_coordinates is not None:
            contract, token_id = items_coordinates
            return await _resolve_verse_items(ref, contract, token_id, settings, client, _depth, origin)
        # Malformed /items/ paths (bad chain segment, address, or token id)
        # are not a recognized Verse shape — fall through to generic HTTP
        # handling below rather than treating them as Verse references.

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
        status, note = await _audit_runtime_html(internal, settings, client)
        return Resolved(
            ref, public, "send", "ipfs", True, content_type=content_type,
            note=note, source_kind="ipfs", final_ref=native_ref, status=status,
            integrity={"algorithm": "ipfs-cid", "digest": cid},
        )

    hinted_ext = ext_from_content_type(content_type)
    if hinted_ext and not query and ext not in KNOWN_EXTENSIONS:
        public = f"{public}?filename=art.{hinted_ext}"
    method = infer_playback_method(path or cid)
    return Resolved(
        ref, public, method, "ipfs", True, content_type=content_type,
        source_kind="ipfs", final_ref=native_ref,
        status=ResolutionStatus.LIVE_DEPENDENT if method == "send" else ResolutionStatus.READY,
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

    internal = f"{settings.arweave_internal.rstrip('/')}/{txid}{path}"
    headers = await probe_headers(client, internal, timeout=settings.arweave_cold_timeout)
    if headers is None:
        return Resolved(
            ref, public, "play", "arweave", False, source_kind="arweave", final_ref=native_ref,
            note="local AR.IO backend cannot serve this artifact",
        )
    content_type = headers.get("content-type")
    main = _main_content_type(content_type)

    integrity = _arweave_integrity(headers)
    if main == "application/json":
        return await _resolve_token_metadata(
            ref, internal, "arweave", settings, client, depth,
            source_kind="arweave", final_ref=native_ref,
        )
    if main == "text/html":
        status, note = await _audit_runtime_html(internal, settings, client)
        return Resolved(
            ref, public, "send", "arweave", True, content_type=content_type,
            note=note, source_kind="arweave", final_ref=native_ref,
            status=status, integrity=integrity,
        )
    return Resolved(
        ref, public, "play", "arweave", True, content_type=content_type,
        source_kind="arweave", final_ref=native_ref,
        status=ResolutionStatus.READY, integrity=integrity,
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
    store = StaticStore(settings.static_root, settings.static_cache_max_bytes)
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
        entry = store.put_file(
            Path(temporary),
            media_type=content_type,
            filename=seg or None,
            source_ref=ref,
            storage_status="stored" if _STORAGE_INTENT.get() else "cached",
        )
        temporary = None  # put_file atomically moved it
    except CacheQuotaError as exc:
        return Resolved(
            ref, ref, "play", "http", False, note=f"media cache admission failed: {exc}",
            source_kind="http", final_ref=ref,
        )
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
        status=ResolutionStatus.LIVE_DEPENDENT if _main_content_type(content_type) == "text/html" else ResolutionStatus.READY,
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


async def read_token_metadata(
    token_uri: str, settings: Settings, client: httpx.AsyncClient
) -> dict[str, Any] | None:
    """Read a current chain-derived token URI as a bounded JSON object.

    This is shared by wallet enumeration and the resolver so Ethereum indexers
    never become the authority for mutable token metadata. None means the
    contract returned a pointer whose metadata cannot currently be read; the
    caller must not silently replace it with an indexer's stale copy.
    """
    match = _DATA_URI_RE.match(token_uri)
    if match is not None:
        params, payload = match.group(1).split(";"), match.group(2)
        mediatype = params[0].strip().lower() or "text/plain"
        if mediatype != "application/json":
            return None
        if len(payload) > settings.data_max_bytes * (4 // 3 + 1) + 16:
            return None
        try:
            text = (
                base64.b64decode(payload, validate=False).decode("utf-8", errors="replace")
                if any(p.strip().lower() == "base64" for p in params[1:])
                else unquote(payload)
            )
            if len(text.encode("utf-8")) > settings.data_max_bytes:
                return None
            metadata: Any = json.loads(text)
        except ValueError:
            return None
        return metadata if isinstance(metadata, dict) else None

    try:
        metadata = json.loads(
            await _bounded_text(
                client, _internal_fetch_url(token_uri, settings), settings.fetch_max_bytes, settings
            )
        )
    except (httpx.HTTPError, ValueError):
        return None
    return metadata if isinstance(metadata, dict) else None


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
    try:
        entry = StaticStore(settings.static_root, settings.static_cache_max_bytes).put(
            data,
            media_type=mediatype,
            filename=None,
            source_ref=ref,
            storage_status="stored" if _STORAGE_INTENT.get() else "cached",
        )
    except CacheQuotaError as exc:
        return Resolved(ref, ref, "play", "data", False, note=f"media cache admission failed: {exc}", final_ref=ref)
    base = origin.rstrip("/") if origin else settings.ipfs_public_base.rstrip("/")
    method = "send" if mediatype == "text/html" else "play"
    return Resolved(ref, f"{base}/media/{entry['id']}", method, "data", True, content_type=mediatype,
        source_kind="data", final_ref=ref, status=ResolutionStatus.LIVE_DEPENDENT if mediatype == "text/html" else ResolutionStatus.READY,
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


def _extract_verse_chain_coordinates(text: str) -> tuple[str, int] | None:
    """(contract address, token id) if the page names both, else None.

    A contract address alone is not a usable coordinate: unsold/primary-market
    Verse artworks carry one with no token minted against it yet.
    """
    contract_match = _CONTRACT_ADDRESS_RE.search(text)
    token_match = _TOKEN_ID_RE.search(text)
    if not contract_match or not token_match:
        return None
    return contract_match.group(1), int(token_match.group(1))


def _verse_items_coordinates(path: str) -> tuple[str, int] | None:
    """(contract address, token id) from a `/items/ethereum/<addr>/<id>` path,
    else None. Only the "ethereum" chain segment is recognized today; a
    different chain, a malformed address, or a non-numeric token id is not
    treated as a Verse chain reference at all — the caller falls through to
    generic URL handling instead.
    """
    match = _ITEMS_PATH_RE.match(path)
    if not match:
        return None
    chain, address, token_id = match.groups()
    if chain != "ethereum" or not _HEX_ADDRESS_RE.match(address) or not token_id.isdigit():
        return None
    return address, int(token_id)


def _encode_uint256_call(selector_hex: str, token_id: int) -> str:
    return f"0x{selector_hex}{token_id:064x}"


def _decode_abi_string(data: bytes) -> str | None:
    """Minimal ABI decode of a single dynamic `string` return value:
    [32B offset][32B length][bytes, right-padded]. Anything short or
    inconsistent decodes to None — a revert or an unexpected return shape
    degrades resolution, it does not raise.
    """
    if len(data) < 64:
        return None
    offset = int.from_bytes(data[0:32], "big")
    if offset + 32 > len(data):
        return None
    length = int.from_bytes(data[offset:offset + 32], "big")
    start = offset + 32
    if start + length > len(data):
        return None
    return data[start:start + length].decode("utf-8", errors="replace")


_ERC721_TOKEN_URI_SELECTOR = "c87b56dd"  # tokenURI(uint256)
_ERC1155_URI_SELECTOR = "0e89341c"  # uri(uint256)


async def _eth_call(client, settings: Settings, contract: str, call_data: str) -> bytes | None:
    """POST a plain JSON-RPC eth_call to the operator-configured endpoint.

    None on any transport error, RPC error, or revert (empty/absent result):
    chain lookups degrade like every other network step in this module, they
    never raise. The RPC URL is operator configuration, not a user-supplied
    ref, so this call skips the SSRF DNS-pinning used for untrusted fetches
    (the same treatment given to `settings.ipfs_api`/`ipfs_internal`).
    """
    try:
        response = await client.post(
            settings.eth_rpc_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_call",
                "params": [{"to": contract, "data": call_data}, "latest"],
            },
            timeout=settings.http_timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if (
        payload.get("error")
        or not isinstance(result, str)
        or not result.startswith("0x")
        or result == "0x"
    ):
        return None
    try:
        return bytes.fromhex(result[2:])
    except (TypeError, ValueError):
        return None


async def ethereum_token_uri(
    client: httpx.AsyncClient,
    settings: Settings,
    contract: str,
    token_id: int,
    standard: str | None = None,
) -> str | None:
    """Read the current ERC-721 tokenURI or ERC-1155 uri from Ethereum.

    A discovered token standard chooses the first selector, avoiding the wrong
    result from hybrid contracts that expose both. Unknown or non-standard
    contracts retain the ERC-721-then-ERC-1155 compatibility fallback. The
    ERC-1155 `{id}` expansion is applied whenever that selector succeeds.
    """
    normalized = (standard or "").upper()
    if normalized == "ERC-1155":
        selectors = (_ERC1155_URI_SELECTOR,)
    elif normalized == "ERC-721":
        selectors = (_ERC721_TOKEN_URI_SELECTOR,)
    else:
        selectors = (_ERC721_TOKEN_URI_SELECTOR, _ERC1155_URI_SELECTOR)
    for selector in selectors:
        data = await _eth_call(
            client, settings, contract, _encode_uint256_call(selector, token_id)
        )
        if data is None:
            continue
        uri = _decode_abi_string(data)
        if uri:
            return (
                uri.replace("{id}", format(token_id, "064x"))
                if selector == _ERC1155_URI_SELECTOR
                else uri
            )
    return None


async def _resolve_token_uri(ref: str, token_uri: str, settings: Settings, client, depth: int) -> Resolved:
    """Resolve a discovered tokenURI (page-scraped or chain-derived) into
    media, tagged as a verse-provider result.

    `_resolve_token_metadata` already fetches ipfs/ar/http metadata and
    recurses into its media, and on failure reports the canonical ref it
    could not reach rather than nothing — exactly the disclosure this needs.
    `data:` URIs carry their metadata inline; `resolve_ref` already knows how
    to decode and recurse into those, so it is reused here instead of
    duplicating that decode.
    """
    if token_uri.startswith("data:"):
        inner = await resolve_ref(token_uri, settings, client, depth + 1)
        return replace(inner, original_ref=ref, provider="verse")

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


def _disclose_dead_chain_ref(result: Resolved, dead_chain: Resolved | None) -> Resolved:
    """Carry forward a chain-found-but-unreachable canonical ref.

    Chain resolution may discover a real tokenURI on-chain and still fail to
    reach its metadata or media (dead pointer, cold storage gone). `result`
    is whatever the scrape fallback produced — playable or not — but the
    catalogue must never look like a scrape rendition (or an empty failure)
    is the canonical source. `dead_chain` is that failed chain `Resolved`;
    its `final_ref` names what SHOULD exist and its `note` says why it
    doesn't.
    """
    if dead_chain is None:
        return result
    outcome = "showing verse scrape fallback" if result.resolved else "no scrape fallback available either"
    disclosure = f"on-chain canonical ref {dead_chain.final_ref} unreachable ({dead_chain.note}); {outcome}"
    note = f"{result.note}; {disclosure}" if result.note else disclosure
    return replace(result, note=note, final_ref=dead_chain.final_ref or result.final_ref)


async def _resolve_verse_chain_or_none(
    ref: str, contract: str, token_id: int, settings: Settings, client, depth: int
) -> Resolved | None:
    """Attempt chain-first resolution for known (contract, token_id)
    coordinates.

    None means chain resolution simply isn't available here (RPC disabled,
    or the call reverted/failed with no usable tokenURI) — the caller treats
    that exactly like "no chain coordinates". A non-None result that is
    itself unresolved means a real on-chain tokenURI WAS found but its
    metadata/media is unreachable — that disclosure must not be dropped.
    """
    if not settings.eth_rpc_url:
        return None
    chain_token_uri = await ethereum_token_uri(client, settings, contract, token_id)
    if not chain_token_uri:
        return None
    return await _resolve_token_uri(ref, chain_token_uri, settings, client, depth)


async def _scrape_verse_media(ref: str, text: str, settings: Settings, client, depth: int) -> Resolved | None:
    """The first of tokenUri / iframeUrl / og:image found in a Verse page's
    markup, resolved into media — or None if the page yields nothing
    playable. Shared by artwork-page and items-URL resolution: both fall
    back to this exact scrape when chain resolution can't be used.
    """
    token_uri = _extract_embedded_value(text, "tokenUri")
    if token_uri:
        return await _resolve_token_uri(ref, token_uri, settings, client, depth)

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

    return None


async def _resolve_verse(
    ref: str, settings: Settings, client, depth: int, origin: str | None
) -> Resolved:
    """Resolve a Verse artwork page, including works that are not minted yet.

    Unminted works may have no contract/token coordinates, so the absence of
    coordinates is not itself an error. In that case the page scrape checks
    embedded tokenUri, then iframeUrl, then og:image. A work that exposes only
    og:image therefore resolves to its static preview until a canonical token
    URI or runtime URL becomes available.
    """
    note = "no tokenUri/iframeUrl/og:image found in verse page"
    dead_chain: Resolved | None = None
    for page_url in _verse_scrape_urls(ref):
        try:
            text = await _bounded_text(client, page_url, settings.fetch_max_bytes, settings)
        except (httpx.HTTPError, ValueError) as exc:
            note = f"verse fetch failed: {exc}"
            continue

        if dead_chain is None:
            coordinates = _extract_verse_chain_coordinates(text)
            if coordinates is not None:
                chain_result = await _resolve_verse_chain_or_none(ref, *coordinates, settings, client, depth)
                if chain_result is not None:
                    if chain_result.resolved:
                        return chain_result
                    # A real on-chain tokenURI was found but its metadata/media
                    # is unreachable: fall through to the scrape chain below
                    # for playability, carrying disclosure of what's missing.
                    dead_chain = chain_result

        scraped = await _scrape_verse_media(ref, text, settings, client, depth)
        if scraped is not None:
            return _disclose_dead_chain_ref(scraped, dead_chain)

    return _disclose_dead_chain_ref(Resolved(ref, ref, "send", "verse", False, note=note), dead_chain)


async def _resolve_verse_items(
    ref: str, contract: str, token_id: int, settings: Settings, client, depth: int, origin: str | None
) -> Resolved:
    """`/items/ethereum/<contract>/<tokenId>` already names its chain
    coordinates in the URL: skip page scraping and go straight to the chain.

    Unlike an artwork page (`_resolve_verse`), there is no guaranteed
    og:image to fall back to here — only the /items/ page itself, fetched
    and scraped the same way, and used only if it actually yields something.
    Total failure names the coordinates that were tried, so the catalogue
    records what was attempted even when nothing plays.
    """
    coordinates_desc = f"contract {contract} token {token_id}"
    chain_result = await _resolve_verse_chain_or_none(ref, contract, token_id, settings, client, depth)
    if chain_result is not None and chain_result.resolved:
        return chain_result
    dead_chain = chain_result

    try:
        text = await _bounded_text(client, ref, settings.fetch_max_bytes, settings)
    except (httpx.HTTPError, ValueError) as exc:
        note = f"chain resolution failed for {coordinates_desc}; items page fetch failed: {exc}"
        return _disclose_dead_chain_ref(Resolved(ref, ref, "play", "verse", False, note=note), dead_chain)

    scraped = await _scrape_verse_media(ref, text, settings, client, depth)
    if scraped is not None:
        return _disclose_dead_chain_ref(scraped, dead_chain)

    note = f"chain resolution failed for {coordinates_desc}; no tokenUri/iframeUrl/og:image found on items page"
    return _disclose_dead_chain_ref(Resolved(ref, ref, "play", "verse", False, note=note), dead_chain)

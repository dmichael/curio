"""Renderer fixups the resolver owns — learned from driving a real FF1,
harmless to other consumers. See docs/design.md and the ff1 repo memory notes.

These encode three quirks:
  1. A bare IPFS CID URL (no filename/extension) renders as an *iframe*, not
     media. Appending `?filename=art.<ext>` forces media rendering.
  2. NFT metadata often lists several image URLs; the biggest bytes wins, and
     field names (`image` vs `image_url`) do NOT reliably indicate which.
     Probe Content-Length rather than trusting names.
  3. "Runtime" works are live HTML/JS and must be *sent* (loaded as a page),
     while static media is *played*. Infer from the URL shape.
"""

from __future__ import annotations

import asyncio
import mimetypes
from urllib.parse import urlparse

import httpx

CONTENT_TYPE_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/avif": "avif",
    "image/svg+xml": "svg",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
}

_SEND_SUFFIXES = (".html", ".htm", "/")

# Extensions trusted without a Content-Type probe: static media plus live-HTML
# works. Anything else that looks like an extension (.php, .v2, dotted routes)
# gets probed — "contains a dot" is not evidence of media.
KNOWN_EXTENSIONS = set(CONTENT_TYPE_EXT.values()) | {
    "jpeg", "bmp", "ico", "tif", "tiff", "apng",
    "mp3", "wav", "ogg", "oga", "ogv", "flac", "m4a", "m4v", "mkv",
    "html", "htm", "pdf", "json",
}


def extension_of(segment: str) -> str | None:
    """The final extension of a path segment, lowercased; None if undotted."""
    if "." not in segment:
        return None
    return segment.rsplit(".", 1)[-1].lower()


def infer_playback_method(url: str) -> str:
    """'send' for HTML/runtime works, 'play' for static media."""
    path = urlparse(url).path.lower()
    return "send" if path.endswith(_SEND_SUFFIXES) else "play"


def ext_from_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    return CONTENT_TYPE_EXT.get(content_type.split(";", 1)[0].strip().lower())


def mint_display_extension(media_path: str, media_type: str | None) -> str:
    """Append the extension implied by media_type to a bare Curio identity
    path: /arweave/<txid>, /ipfs/<cid>, or /media/<id>, with no inner path
    and no extension yet.

    Arweave txids, IPFS CIDs, and Curio's own uuid4 media ids never contain
    a dot, so a dot already in the identity segment unambiguously means an
    extension is there; a slash means a manifest/inner path follows. Either
    way, the path is left untouched — as it is when media_type is unknown.
    """
    if not media_type:
        return media_path
    for prefix in ("/arweave/", "/ipfs/", "/media/"):
        if not media_path.startswith(prefix):
            continue
        identity, sep, tail = media_path[len(prefix):].partition("?")
        if "/" in identity or "." in identity:
            return media_path
        main = media_type.split(";", 1)[0].strip().lower()
        ext = CONTENT_TYPE_EXT.get(main) or (mimetypes.guess_extension(main) or "").lstrip(".")
        return f"{prefix}{identity}.{ext}{sep}{tail}" if ext else media_path
    return media_path


async def probe_headers(
    client: httpx.AsyncClient, url: str, *, timeout: float | None = None
) -> httpx.Headers | None:
    """HEAD `url` and return its headers, or None on any failure.

    Probes are best-effort: a down gateway degrades a fixup, never the resolve.
    Native AR.IO callers pass their separate cold-read timeout; all other
    probes retain the client's normal timeout.
    """
    try:
        response = await client.head(url, timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    return response.headers


def _content_length(headers: httpx.Headers | None) -> int:
    if headers is None:
        return -1
    try:
        return int(headers["content-length"])
    except (KeyError, ValueError):
        return -1


async def pick_largest(
    candidates: list[tuple[str, str]], client: httpx.AsyncClient
) -> str:
    """Return the ref whose bytes are biggest, from (ref, probe_url) pairs.

    Field names don't reliably say which image variant is the full-size one;
    Content-Length does. Falls back to the first ref when every probe fails.
    """
    headers = await asyncio.gather(
        *(probe_headers(client, probe_url) for _, probe_url in candidates)
    )
    sizes = [_content_length(h) for h in headers]
    best = max(range(len(candidates)), key=lambda i: sizes[i])
    return candidates[best][0] if sizes[best] >= 0 else candidates[0][0]

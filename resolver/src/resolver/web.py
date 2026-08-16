"""Small human-facing web pages for the Curio appliance."""

from __future__ import annotations

import hashlib
import html
from importlib import resources
from urllib.parse import parse_qs, unquote, urlsplit

from .origin import normalize_origin
from .refs import canonical_ref_key
from .static_store import StaticStore, playable

_PAGE_HEADERS = {
    "cache-control": "no-cache",
    "content-security-policy": (
        "default-src 'none'; style-src 'self'; script-src 'self'; "
        "img-src 'self' data:; media-src 'self'; frame-src 'self'; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'"
    ),
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
}

_ASSETS = resources.files(__package__).joinpath("web_assets")


def _read_asset(name: str) -> str:
    return _ASSETS.joinpath(name).read_text(encoding="utf-8")


STYLES = _read_asset("curio.css")
DISPLAY_SCRIPT = _read_asset("display.js")
_ASSET_VERSION = hashlib.sha256(f"{STYLES}\n{DISPLAY_SCRIPT}".encode()).hexdigest()[:12]


def page_headers() -> dict[str, str]:
    return dict(_PAGE_HEADERS)


def homepage(version: str, *, error: str | None = None, uri: str = "") -> str:
    return (
        _read_asset("index.html")
        .replace("{{asset_version}}", _ASSET_VERSION)
        .replace("{{version}}", html.escape(version))
        .replace("{{error_hidden}}", "" if error else "hidden")
        .replace("{{error}}", html.escape(error or ""))
        .replace("{{uri}}", html.escape(uri, quote=True))
    )


def display_page(uri: str) -> str:
    return (
        _read_asset("display.html")
        .replace("{{asset_version}}", _ASSET_VERSION)
        .replace("{{media_uri}}", html.escape(uri, quote=True))
    )


def validate_display_uri(
    value: str,
    *,
    origin: str,
    store: StaticStore,
) -> tuple[str | None, str | None]:
    """Return a normalized same-origin playback URI or a user-facing error."""
    try:
        parsed = urlsplit(value.strip())
        target_origin = normalize_origin(f"{parsed.scheme}://{parsed.netloc}")
    except ValueError:
        return None, "The display URI is malformed."
    if (
        target_origin is None
        or target_origin != normalize_origin(origin)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None, "Display accepts only media served by this Curio."

    path = parsed.path
    lower_path = path.lower()
    if "%2f" in lower_path or "%5c" in lower_path or "\\" in path:
        return None, "The display media path is malformed."
    decoded_segments = unquote(path).split("/")
    if any(segment in {".", ".."} for segment in decoded_segments):
        return None, "The display media path is malformed."

    if path == "/resolve":
        query = parse_qs(parsed.query, keep_blank_values=True)
        if set(query) != {"ref"} or len(query["ref"]) != 1 or not query["ref"][0].strip():
            return None, "The Curio resolve URI is malformed."
        record = store.resolution(canonical_ref_key(query["ref"][0]))
        if record is None or not playable(record):
            return None, "That saved Curio reference is not playable."
    elif path.startswith("/media/"):
        identity = path.removeprefix("/media/")
        if not identity or "/" in identity:
            return None, "The Curio media URI is malformed."
        file_id = identity.split(".", 1)[0]
        if store.get(file_id) is None:
            return None, "That Curio media object is not available."
    elif path.startswith(("/ipfs/", "/arweave/")):
        if len(path.split("/", 2)) < 3 or not path.split("/", 2)[2]:
            return None, "The Curio media URI is malformed."
    else:
        return None, "Display accepts only Curio playback routes."

    normalized = f"{target_origin}{path}"
    if parsed.query:
        normalized += f"?{parsed.query}"
    return normalized, None

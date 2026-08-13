"""Curio's own serving URLs carry a display extension derived from the
catalogue (mint_display_extension), and the proxy/media routes strip it
back off before dispatching to identity (_strip_display_extension /
/media/{file_id}). Canonical refs (ar://, ipfs://) are never touched by
either mechanism — only the media_path Curio mints for its own routes.
"""

import httpx

from resolver import app as app_module
from resolver import operations
from resolver.config import get_settings
from resolver.fixups import mint_display_extension
from resolver.resolve import Resolved


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---- mint_display_extension: pure function ----


def test_mint_appends_known_extension_to_bare_arweave_txid():
    assert mint_display_extension("/arweave/TXID", "image/png") == "/arweave/TXID.png"


def test_mint_leaves_bare_txid_unminted_for_unknown_or_missing_type():
    assert mint_display_extension("/arweave/TXID", "application/x-mystery") == "/arweave/TXID"
    assert mint_display_extension("/arweave/TXID", None) == "/arweave/TXID"


def test_mint_appends_extension_to_bare_ipfs_cid():
    assert mint_display_extension("/ipfs/CID", "video/mp4") == "/ipfs/CID.mp4"


def test_mint_leaves_manifest_and_inner_paths_untouched():
    assert (
        mint_display_extension("/arweave/TXID/metadata.json", "application/json")
        == "/arweave/TXID/metadata.json"
    )
    assert mint_display_extension("/ipfs/CID/art/1.png", "image/png") == "/ipfs/CID/art/1.png"


def test_mint_leaves_already_dotted_path_untouched():
    assert mint_display_extension("/arweave/TXID.png", "image/png") == "/arweave/TXID.png"


def test_mint_appends_extension_to_bare_media_id():
    assert mint_display_extension("/media/abc123", "image/jpeg") == "/media/abc123.jpg"


# ---- proxy stripping: /arweave and /ipfs ----


def test_proxy_strips_display_extension_from_first_segment(http_client, monkeypatch):
    monkeypatch.setenv("RESOLVER_ARWEAVE_INTERNAL", "http://ar.internal")
    get_settings.cache_clear()
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        # MockTransport's `content=` shortcut eagerly reads the body, which
        # would leave the real proxy's `stream=True` + `aiter_raw()` call
        # finding an already-consumed stream. `stream=` defers that read.
        return httpx.Response(
            200, headers={"content-type": "image/png"}, stream=httpx.ByteStream(b"bytes")
        )

    real = app_module.app.state.client
    app_module.app.state.client = _mock_client(handler)
    try:
        response = http_client.get("/arweave/TXID.png")
    finally:
        app_module.app.state.client = real
    assert response.status_code == 200
    assert requested == ["http://ar.internal/TXID"]
    # Media bytes are public web resources; browser blob loaders need this.
    assert response.headers["access-control-allow-origin"] == "*"
    get_settings.cache_clear()


def test_proxy_leaves_manifest_path_unchanged(http_client, monkeypatch):
    monkeypatch.setenv("RESOLVER_ARWEAVE_INTERNAL", "http://ar.internal")
    get_settings.cache_clear()
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        # MockTransport's `content=` shortcut eagerly reads the body, which
        # would leave the real proxy's `stream=True` + `aiter_raw()` call
        # finding an already-consumed stream. `stream=` defers that read.
        return httpx.Response(
            200, headers={"content-type": "image/png"}, stream=httpx.ByteStream(b"bytes")
        )

    real = app_module.app.state.client
    app_module.app.state.client = _mock_client(handler)
    try:
        response = http_client.get("/arweave/TXID/inner/file.png")
    finally:
        app_module.app.state.client = real
    assert response.status_code == 200
    assert requested == ["http://ar.internal/TXID/inner/file.png"]
    get_settings.cache_clear()


# ---- end-to-end: GET /resolve redirect target carries the extension ----


def test_post_stores_arweave_and_resolve_redirect_carries_extension(
    http_client, tmp_path, monkeypatch
):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()

    async def resolved(ref, *_args, **_kwargs):
        return Resolved(
            ref,
            "http://curio.example/arweave/TXIDARWEAVE",
            "play",
            "arweave",
            True,
            content_type="image/png",
            source_kind="arweave",
            final_ref="ar://TXIDARWEAVE",
        )

    async def pin(*_args, **_kwargs):
        return "pinned"

    monkeypatch.setattr(operations, "resolve_ref", resolved)
    monkeypatch.setattr(operations, "store_resolved", pin)
    response = http_client.post(
        "/resolve", params={"ref": "ar://TXIDARWEAVE"}, headers={"Host": "curio.example"}
    )
    assert response.status_code == 200
    assert response.json()["media_type"] == "image/png"

    playback = http_client.get(
        "/resolve", params={"ref": "ar://TXIDARWEAVE"}, follow_redirects=False
    )
    assert playback.status_code == 302
    assert playback.headers["location"] == "/arweave/TXIDARWEAVE.png"
    get_settings.cache_clear()


# ---- /media: minted at upload time, stripped before the store lookup ----


def test_uploaded_media_url_carries_extension_and_both_paths_serve_the_same_object(
    http_client, tmp_path, monkeypatch
):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()

    response = http_client.post(
        "/resolve", files={"file": ("piece.png", b"png-bytes", "image/png")}
    )
    assert response.status_code == 201

    playback = http_client.get(
        "/resolve",
        params={"ref": response.json()["ref"]},
        follow_redirects=False,
    )
    assert playback.status_code == 302
    location = playback.headers["location"]
    assert location.startswith("/media/") and location.endswith(".png")
    bare = location[: -len(".png")]

    with_ext = http_client.get(location)
    without_ext = http_client.get(bare)
    assert with_ext.status_code == without_ext.status_code == 200
    assert with_ext.content == without_ext.content == b"png-bytes"
    assert with_ext.headers["content-type"] == without_ext.headers["content-type"] == "image/png"
    # Media bytes are public web resources; browser blob loaders need this.
    assert with_ext.headers["access-control-allow-origin"] == "*"
    get_settings.cache_clear()

"""Target media-model contracts, independent of real gateways."""
import socket

import httpx

from resolver import app as app_module
from resolver import mcp_server
from resolver.config import Settings, get_settings
from resolver.resolve import Resolved, resolve_ref
from resolver.static_store import StaticStore


async def test_http_is_static_same_origin_and_never_calls_kubo(tmp_path):
    settings = Settings(static_root=str(tmp_path), ipfs_api="http://kubo.internal", ssrf_dns_check=False)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert "kubo.internal" not in str(request.url)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"png")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await resolve_ref("https://origin.example/piece.png", settings, client,
                                   origin="https://curio.example")
    assert result.resolved_url.startswith("https://curio.example/media/")
    assert result.source_kind == "http"
    assert result.integrity and result.integrity["algorithm"] == "sha256"
    assert calls == ["https://origin.example/piece.png"]


def test_static_duplicate_file_consumes_temporary(tmp_path):
    store = StaticStore(str(tmp_path))
    first = store.put(b"same", media_type="image/png", filename="a.png", source_ref="one")
    temporary = tmp_path / ".fetch-duplicate"
    temporary.write_bytes(b"same")
    second = store.put_file(temporary, media_type="image/png", filename="b.png", source_ref="two")
    assert first["digest"] == second["digest"]
    assert not temporary.exists()


def test_static_keep_survives_store_reopen(tmp_path):
    store = StaticStore(str(tmp_path))
    item = store.put(b"original", media_type="image/png", filename="piece.png", source_ref="https://x")
    assert store.keep(str(item["id"]))
    reopened = StaticStore(str(tmp_path)).get(str(item["id"]))
    assert reopened is not None
    assert reopened[0]["keep_state"] == "kept"
    assert reopened[1].read_bytes() == b"original"


def test_data_media_is_static_not_a_data_url(tmp_path):
    entry = StaticStore(str(tmp_path)).put(b"<svg/>", media_type="image/svg+xml", filename=None,
                                            source_ref="data:image/svg+xml,...")
    assert StaticStore(str(tmp_path)).get(str(entry["id"])) is not None


def test_mutation_rejects_wrong_curator_token(http_client):
    response = http_client.post("/store", headers={"Authorization": "Bearer wrong"},
                                files={"file": ("x.txt", b"x", "text/plain")})
    assert response.status_code == 401


def test_request_origin_is_used_for_ipfs(http_client):
    response = http_client.get("/resolve", params={"ref": "ipfs://bafyCID/a.png"},
                               headers={"Host": "curio.example"})
    assert response.status_code == 200
    assert response.json()["media_url"] == "http://curio.example/ipfs/bafyCID/a.png"


async def test_redirect_revalidates_each_hop_and_never_connects_private(monkeypatch, tmp_path):
    settings = Settings(static_root=str(tmp_path), ipfs_api="http://kubo", redirect_max_hops=2)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
    ])
    calls = []
    def handler(request):
        calls.append((str(request.url), request.headers["host"], request.extensions.get("sni_hostname")))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await resolve_ref("https://safe.example/a.png", settings, client, origin="https://curio.example")
    assert result.resolved is False
    assert calls == [("https://8.8.8.8/a.png", "safe.example", "safe.example")]


async def test_dns_connection_is_pinned_not_independently_resolved(monkeypatch, tmp_path):
    settings = Settings(static_root=str(tmp_path))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.4.4", 443))
    ])
    seen = []
    def handler(request):
        seen.append((str(request.url), request.headers["host"], request.extensions.get("sni_hostname")))
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"x")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await resolve_ref("https://rebind.example/p.png", settings, client, origin="https://curio.example")
    assert result.resolved
    assert seen == [("https://8.8.4.4/p.png", "rebind.example", "rebind.example")]


def test_resolve_pin_promotes_static_synchronously(http_client, tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()
    entry = StaticStore(str(tmp_path / "media")).put(b"x", media_type="image/png", filename="x.png", source_ref="x")
    async def static_result(*_args, **_kwargs):
        return Resolved("x", f"http://testserver/media/{entry['id']}", "play", "http", True, source_kind="http")
    monkeypatch.setattr(app_module, "resolve_ref", static_result)
    monkeypatch.setattr(app_module, "pin_in_background", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no background pin")))
    response = http_client.get("/resolve", params={"ref": "https://origin.example/x.png", "pin": "true"})
    assert response.status_code == 200
    assert response.json()["promoted"] is True and response.json()["keep_state"] == "kept"
    assert response.json()["pin_scheduled"] is False
    get_settings.cache_clear()


def test_favorite_promotes_static_and_does_not_schedule_ipfs(http_client, tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_FAVORITES_PATH", str(tmp_path / "favorites.json"))
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()
    entry = StaticStore(str(tmp_path / "media")).put(b"x", media_type="image/png", filename="x.png", source_ref="x")
    async def static_result(*_args, **_kwargs):
        return Resolved("x", f"http://testserver/media/{entry['id']}", "play", "http", True, source_kind="http")
    monkeypatch.setattr(app_module, "resolve_ref", static_result)
    monkeypatch.setattr(app_module, "pin_in_background", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no IPFS helper")))
    response = http_client.post("/favorites", params={"ref": "https://origin.example/x.png"})
    assert response.status_code == 201 and response.json()["promoted"] is True
    assert StaticStore(str(tmp_path / "media")).get(str(entry["id"]))[0]["keep_state"] == "kept"
    get_settings.cache_clear()


def test_favorite_uses_arweave_retained_plane(http_client, tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_FAVORITES_PATH", str(tmp_path / "favorites.json"))
    get_settings.cache_clear()
    async def ar_result(*_args, **_kwargs):
        return Resolved("ar://x", "http://testserver/arweave/x", "play", "arweave", True, source_kind="arweave")
    monkeypatch.setattr(app_module, "resolve_ref", ar_result)
    async def retained(*_args, **_kwargs): return "kept"
    monkeypatch.setattr(app_module, "pin_resolved", retained)
    response = http_client.post("/favorites", params={"ref": "ar://x"})
    assert response.status_code == 201
    assert response.json()["pin_scheduled"] is False
    assert response.json()["keep_state"] == "kept"
    get_settings.cache_clear()


def test_keep_does_not_lie_when_ipfs_pin_fails(http_client, monkeypatch):
    async def ipfs_result(*_args, **_kwargs):
        return Resolved("ipfs://x", "http://testserver/ipfs/bafy/x.png", "play", "ipfs", True, source_kind="ipfs")
    async def fail_pin(*_args, **_kwargs):
        raise httpx.ConnectError("pin down")
    monkeypatch.setattr(app_module, "resolve_ref", ipfs_result)
    monkeypatch.setattr(app_module, "pin_resolved", fail_pin)
    response = http_client.post("/keep", params={"ref": "ipfs://x"})
    assert response.status_code == 502 and response.json()["keep_state"] == "failed"


def test_html_capture_is_live_dependent_and_keep_refuses(http_client, monkeypatch):
    async def html_result(*_args, **_kwargs):
        return Resolved("x", "http://testserver/media/x", "send", "http", True, source_kind="http", keep_state="live-dependent")
    monkeypatch.setattr(app_module, "resolve_ref", html_result)
    response = http_client.post("/keep", params={"ref": "https://origin.example/work.html"})
    assert response.status_code == 409 and response.json()["keep_state"] == "live-dependent"


async def test_mcp_uses_configured_public_origin(monkeypatch):
    monkeypatch.setenv("RESOLVER_PUBLIC_BASE_URL", "https://curio.public")
    get_settings.cache_clear()
    def handler(_request): raise AssertionError("no network")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        mcp_server.set_client(client)
        content, _ = await mcp_server.mcp.call_tool("resolve", {"ref": "ipfs://bafyCID/a.png"})
    assert "https://curio.public/ipfs/bafyCID/a.png" in content[0].text
    get_settings.cache_clear()

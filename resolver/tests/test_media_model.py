"""Target media-model contracts, independent of real gateways."""
import json
import multiprocessing
import socket
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from resolver import app as app_module
from resolver import mcp_server
from resolver.config import Settings, get_settings
from resolver.library import pin_resolved
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
    source = "data:image/svg+xml," + ("x" * 1_000_000)
    entry = StaticStore(str(tmp_path)).put(b"<svg/>", media_type="image/svg+xml", filename=None,
                                            source_ref=source)
    reopened = StaticStore(str(tmp_path))
    assert reopened.get(str(entry["id"])) is not None
    db = reopened._connection()
    try:
        stored = db.execute("SELECT source_ref FROM media WHERE id = ?", (entry["id"],)).fetchone()[0]
    finally:
        db.close()
    assert stored == f"data:sha256:{entry['digest']}"


def test_static_cache_evicts_lru_but_never_kept_objects(tmp_path):
    store = StaticStore(str(tmp_path), cache_max_bytes=8)
    first = store.put(b"aaaa", media_type=None, filename=None, source_ref="one")
    second = store.put(b"bbbb", media_type=None, filename=None, source_ref="two")
    # Accessing first makes second the least recently used cached object.
    assert store.get(str(first["id"])) is not None
    third = store.put(b"cccc", media_type=None, filename=None, source_ref="three")
    assert store.get(str(first["id"])) is not None
    assert store.get(str(second["id"])) is None
    assert store.get(str(third["id"])) is not None
    assert store.keep(str(first["id"]))
    fourth = store.put(b"dddd", media_type=None, filename=None, source_ref="four")
    fifth = store.put(b"eeee", media_type=None, filename=None, source_ref="five")
    assert store.get(str(first["id"])) is not None  # kept can exceed cache quota
    assert store.get(str(third["id"])) is None
    assert store.get(str(fourth["id"])) is not None
    assert store.get(str(fifth["id"])) is not None


async def test_static_cache_refuses_unadmittable_media_honestly(tmp_path):
    settings = Settings(static_root=str(tmp_path), static_cache_max_bytes=3, ssrf_dns_check=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"content-type": "image/png"}, content=b"four")
    )) as client:
        result = await resolve_ref("https://origin.example/too-large.png", settings, client)
    assert not result.resolved
    assert "cache admission failed" in (result.note or "")


def test_static_store_deduplicates_concurrent_identity_inserts(tmp_path):
    def put() -> dict[str, object]:
        return StaticStore(str(tmp_path), cache_max_bytes=32).put(
            b"same", media_type="image/png", filename="x.png", source_ref="https://example/x.png",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        entries = list(pool.map(lambda _: put(), range(2)))
    assert {entry["id"] for entry in entries} == {entries[0]["id"]}
    store = StaticStore(str(tmp_path), cache_max_bytes=32)
    db = store._connection()
    try:
        assert db.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1
    finally:
        db.close()


def _open_cold_static_store(root: str, barrier, results) -> None:
    try:
        barrier.wait(timeout=10)
        db = StaticStore(root)._connection()
        try:
            results.put((db.execute("PRAGMA journal_mode").fetchone()[0], None))
        finally:
            db.close()
    except Exception as error:
        results.put((None, repr(error)))


def test_static_store_repeated_concurrent_cold_starts(tmp_path):
    context = multiprocessing.get_context("spawn")
    workers = 4
    for attempt in range(8):
        barrier = context.Barrier(workers)
        results = context.Queue()
        processes = [
            context.Process(
                target=_open_cold_static_store,
                args=(str(tmp_path / f"cold-start-{attempt}"), barrier, results),
            )
            for _ in range(workers)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
        try:
            assert [process.exitcode for process in processes] == [0] * workers
            assert [results.get(timeout=2) for _ in processes] == [("wal", None)] * workers
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join()


def test_static_store_initializes_schema_once_per_process(tmp_path, monkeypatch):
    calls = 0
    migrate = StaticStore._migrate

    def counted_migrate(self, db):
        nonlocal calls
        calls += 1
        migrate(self, db)

    monkeypatch.setattr(StaticStore, "_migrate", counted_migrate)
    for _ in range(2):
        db = StaticStore(str(tmp_path))._connection()
        db.close()
    assert calls == 1


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


def test_favorite_verifies_same_core_arweave_cache(http_client, tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_FAVORITES_PATH", str(tmp_path / "favorites.json"))
    get_settings.cache_clear()
    async def ar_result(*_args, **_kwargs):
        return Resolved("ar://x", "http://testserver/arweave/x", "play", "arweave", True, source_kind="arweave")
    monkeypatch.setattr(app_module, "resolve_ref", ar_result)
    async def verified(*_args, **_kwargs): return "kept"
    monkeypatch.setattr(app_module, "pin_resolved", verified)
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


async def test_pin_helper_refuses_live_runtime_html(tmp_path):
    settings = Settings(ipfs_api="http://kubo.internal")
    runtime = Resolved(
        "ipfs://bafyRUNTIME/index.html", "http://box/ipfs/bafyRUNTIME/index.html", "send", "ipfs", True,
        source_kind="ipfs", final_ref="ipfs://bafyRUNTIME/index.html", keep_state="live-dependent",
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(AssertionError(f"unexpected keep: {request.url}"))
    )) as client:
        assert await pin_resolved(runtime, settings, client) == "live-dependent"


def test_html_capture_is_live_dependent_and_keep_refuses(http_client, monkeypatch):
    async def html_result(*_args, **_kwargs):
        return Resolved("x", "http://testserver/media/x", "send", "http", True, source_kind="http", keep_state="live-dependent")
    monkeypatch.setattr(app_module, "resolve_ref", html_result)
    response = http_client.post("/keep", params={"ref": "https://origin.example/work.html"})
    assert response.status_code == 409 and response.json()["keep_state"] == "live-dependent"


@pytest.mark.parametrize("source_kind", ["ipfs", "arweave"])
def test_live_native_html_is_never_scheduled_or_kept_by_rest_actions(
    http_client, tmp_path, monkeypatch, source_kind
):
    monkeypatch.setenv("RESOLVER_FAVORITES_PATH", str(tmp_path / "favorites.json"))
    get_settings.cache_clear()
    final_ref = "ipfs://bafyRUNTIME/index.html" if source_kind == "ipfs" else f"ar://{'H' * 43}/index.html"

    async def html_result(*_args, **_kwargs):
        return Resolved(
            final_ref, "http://testserver/runtime", "send", source_kind, True,
            source_kind=source_kind, final_ref=final_ref, keep_state="live-dependent",
        )

    def retention_called(*_args, **_kwargs):
        raise AssertionError("runtime HTML must not be retained as a complete work")

    monkeypatch.setattr(app_module, "resolve_ref", html_result)
    monkeypatch.setattr(app_module, "pin_in_background", retention_called)
    monkeypatch.setattr(app_module, "pin_resolved", retention_called)
    response = http_client.get("/resolve", params={"ref": final_ref, "pin": "true"})
    assert response.status_code == 200
    assert response.json()["keep_state"] == "live-dependent"
    assert response.json()["pin_scheduled"] is False
    response = http_client.post("/favorites", params={"ref": final_ref})
    assert response.status_code == 201
    assert response.json()["keep_state"] == "live-dependent"
    assert response.json()["pin_scheduled"] is False
    get_settings.cache_clear()


@pytest.mark.parametrize("source_kind", ["ipfs", "arweave"])
async def test_mcp_live_html_is_never_scheduled_or_kept(tmp_path, monkeypatch, source_kind):
    monkeypatch.setenv("RESOLVER_FAVORITES_PATH", str(tmp_path / "favorites.json"))
    get_settings.cache_clear()
    mcp_server.get_favorites.cache_clear()
    final_ref = "ipfs://bafyRUNTIME/index.html" if source_kind == "ipfs" else f"ar://{'M' * 43}/index.html"

    async def html_result(*_args, **_kwargs):
        return Resolved(
            final_ref, "http://testserver/runtime", "send", source_kind, True,
            source_kind=source_kind, final_ref=final_ref, keep_state="live-dependent",
        )

    def retention_called(*_args, **_kwargs):
        raise AssertionError("runtime HTML must not be retained as a complete work")

    monkeypatch.setattr(mcp_server, "resolve_ref", html_result)
    monkeypatch.setattr(mcp_server, "pin_in_background", retention_called)
    monkeypatch.setattr(mcp_server, "pin_resolved", retention_called)
    content, _ = await mcp_server.mcp.call_tool(
        "resolve", {"ref": final_ref, "pin": True, "curator_token": "test-curator-token"},
    )
    resolved = json.loads(content[0].text)
    assert resolved["keep_state"] == "live-dependent"
    assert resolved["pin_scheduled"] is False
    content, _ = await mcp_server.mcp.call_tool(
        "add_favorite", {"ref": final_ref, "curator_token": "test-curator-token"},
    )
    favorite = json.loads(content[0].text)
    assert favorite["keep_state"] == "live-dependent"
    assert favorite["pin_scheduled"] is False
    get_settings.cache_clear()
    mcp_server.get_favorites.cache_clear()


async def test_mcp_uses_configured_public_origin(monkeypatch):
    monkeypatch.setenv("RESOLVER_PUBLIC_BASE_URL", "https://curio.public")
    get_settings.cache_clear()
    def handler(request):
        if request.method == "HEAD" and request.url.host == "127.0.0.1":
            return httpx.Response(200, headers={"content-type": "image/png"})
        raise AssertionError("unexpected network")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        mcp_server.set_client(client)
        content, _ = await mcp_server.mcp.call_tool("resolve", {"ref": "ipfs://bafyCID/a.png"})
    assert "https://curio.public/ipfs/bafyCID/a.png" in content[0].text
    get_settings.cache_clear()

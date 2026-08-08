import asyncio

import httpx

from resolver import app as app_module
from resolver.arweave_retention import keep_arweave, retained_available, retained_records
from resolver.config import Settings, get_settings
from resolver.resolve import resolve_ref


async def test_retained_hydration_is_transactional_and_survives_settings_reopen(tmp_path):
    settings = Settings(
        arweave_retained_internal="http://retained.internal",
        arweave_retention_db=str(tmp_path / "retained.sqlite3"),
    )
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        return httpx.Response(200, content=b"original bytes")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await keep_arweave("A" * 43, "/manifest/item.png", settings, client) == "kept"
        assert await retained_available("A" * 43, "/manifest/item.png", settings, client)
    # Two full GETs prove hydration then local native availability; the sqlite
    # registry is independent of the resolver process/settings object.
    assert calls[:2] == [
        ("GET", "http://retained.internal/" + "A" * 43 + "/manifest/item.png"),
        ("GET", "http://retained.internal/" + "A" * 43 + "/manifest/item.png"),
    ]
    reopened = Settings(arweave_retention_db=str(tmp_path / "retained.sqlite3"))
    assert retained_records(reopened)[0]["state"] == "kept"


async def test_retained_hydration_failure_never_marks_kept(tmp_path):
    settings = Settings(arweave_retained_internal="http://retained.internal", arweave_retention_db=str(tmp_path / "r.sqlite3"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
        assert await keep_arweave("B" * 43, "", settings, client) == "failed"
    record = retained_records(settings)[0]
    assert record["state"] == "failed" and record["error"]


async def test_resolve_reports_kept_only_when_retained_plane_confirms(tmp_path):
    settings = Settings(
        arweave_internal="http://ordinary.internal",
        arweave_retained_internal="http://retained.internal",
        arweave_retention_db=str(tmp_path / "r.sqlite3"),
    )
    txid = "C" * 43

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ordinary.internal":
            return httpx.Response(200, headers={"content-type": "image/png"})
        return httpx.Response(200, content=b"kept")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await keep_arweave(txid, "", settings, client) == "kept"
        result = await resolve_ref(f"ar://{txid}", settings, client, origin="https://curio.example")
    assert result.keep_state == "kept"
    assert result.resolved_url == f"https://curio.example/arweave/{txid}"


def test_public_kept_txid_routes_to_retained_core(http_client, tmp_path, monkeypatch):
    txid = "D" * 43
    monkeypatch.setenv("RESOLVER_ARWEAVE_RETAINED_INTERNAL", "http://retained.internal")
    monkeypatch.setenv("RESOLVER_ARWEAVE_INTERNAL", "http://ordinary.internal")
    monkeypatch.setenv("RESOLVER_ARWEAVE_RETENTION_DB", str(tmp_path / "r.sqlite3"))
    get_settings.cache_clear()

    async def mark_kept():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x"))) as client:
            await keep_arweave(txid, "", get_settings(), client)

    asyncio.run(mark_kept())
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(200, headers={"content-type": "image/png"}, stream=httpx.ByteStream(b"original"))

    original = app_module.app.state.client
    app_module.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        response = http_client.get(f"/arweave/{txid}")
    finally:
        app_module.app.state.client = original
        get_settings.cache_clear()
    assert response.status_code == 200 and response.content == b"original"
    assert seen == ["retained.internal"]

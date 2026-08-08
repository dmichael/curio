import asyncio
from urllib.parse import urlparse

import httpx

from resolver import app as app_module
from resolver.arweave_retention import (
    keep_arweave,
    retained_available,
    retained_records,
    retained_state,
)
from resolver.config import Settings, get_settings
from resolver.library import pin_resolved
from resolver.resolve import resolve_ref


async def test_retained_hydration_is_transactional_and_survives_settings_reopen(tmp_path):
    settings = Settings(
        arweave_retained_internal="http://retained.internal",
        arweave_retention_db=str(tmp_path / "retained.sqlite3"),
    )
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        return httpx.Response(200, headers={"x-cache": "HIT"}, content=b"original bytes")

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


async def test_retained_hydration_without_native_hit_never_marks_kept(tmp_path):
    settings = Settings(arweave_retained_internal="http://retained.internal", arweave_retention_db=str(tmp_path / "r.sqlite3"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"x-cache": "MISS"}, content=b"upstream")
    )) as client:
        assert await keep_arweave("M" * 43, "", settings, client) == "failed"
    record = retained_records(settings)[0]
    assert record["state"] == "failed" and "X-Cache: HIT" in str(record["error"])


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
        return httpx.Response(200, headers={"x-cache": "HIT"}, content=b"kept")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await keep_arweave(txid, "", settings, client) == "kept"
        result = await resolve_ref(f"ar://{txid}", settings, client, origin="https://curio.example")
    assert result.keep_state == "kept"
    assert result.resolved_url == f"https://curio.example/arweave/{txid}"


async def test_retained_state_is_exact_to_the_manifest_path(tmp_path):
    settings = Settings(arweave_retained_internal="http://retained.internal", arweave_retention_db=str(tmp_path / "r.sqlite3"))
    txid = "P" * 43
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"x-cache": "HIT"}, content=b"x")
    )) as client:
        assert await keep_arweave(txid, "/one.png", settings, client) == "kept"
    assert retained_state(txid, "/one.png", settings) == "kept"
    assert retained_state(txid, "/other.png", settings) is None


async def test_metadata_keeps_its_final_arweave_media_identity(tmp_path):
    settings = Settings(
        arweave_internal="http://ordinary.internal",
        arweave_retained_internal="http://retained.internal",
        arweave_retention_db=str(tmp_path / "r.sqlite3"),
    )
    metadata, media = "Q" * 43, "R" * 43
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        if request.url.host == "ordinary.internal" and request.url.path == f"/{metadata}":
            if request.method == "HEAD":
                return httpx.Response(200, headers={"content-type": "application/json"})
            return httpx.Response(200, json={"image": f"ar://{media}"})
        if request.url.host == "ordinary.internal" and request.url.path == f"/{media}":
            return httpx.Response(200, headers={"content-type": "image/png"})
        if request.url.host == "retained.internal" and request.url.path == f"/{media}":
            return httpx.Response(200, headers={"x-cache": "HIT"}, content=b"media")
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await resolve_ref(f"ar://{metadata}", settings, client)
        assert result.original_ref == f"ar://{metadata}"
        assert result.final_ref == f"ar://{media}"
        assert await pin_resolved(result, settings, client) == "kept"
    assert retained_state(metadata, "", settings) is None
    assert retained_state(media, "", settings) == "kept"
    assert not any(urlparse(url).hostname == "retained.internal" and urlparse(url).path == f"/{metadata}" for _, url in seen)


async def test_kept_resolution_uses_retained_core_without_ordinary_fallback(tmp_path):
    txid = "S" * 43
    settings = Settings(
        arweave_internal="http://ordinary.internal",
        arweave_retained_internal="http://retained.internal",
        arweave_retention_db=str(tmp_path / "r.sqlite3"),
    )

    def retained(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ordinary.internal":
            raise AssertionError("kept identity fell back to ordinary Core")
        return httpx.Response(200, headers={"x-cache": "HIT", "content-type": "image/png"}, content=b"kept")

    async with httpx.AsyncClient(transport=httpx.MockTransport(retained)) as client:
        assert await keep_arweave(txid, "", settings, client) == "kept"
        result = await resolve_ref(f"ar://{txid}", settings, client)
    assert result.resolved and result.keep_state == "kept"


async def test_kept_resolution_degrades_when_retained_hit_is_gone(tmp_path):
    txid = "T" * 43
    settings = Settings(
        arweave_internal="http://ordinary.internal",
        arweave_retained_internal="http://retained.internal",
        arweave_retention_db=str(tmp_path / "r.sqlite3"),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"x-cache": "HIT"}, content=b"kept")
    )) as client:
        assert await keep_arweave(txid, "", settings, client) == "kept"

    def unavailable(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ordinary.internal":
            raise AssertionError("kept identity fell back to ordinary Core")
        return httpx.Response(200, headers={"x-cache": "MISS"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        result = await resolve_ref(f"ar://{txid}", settings, client)
    assert not result.resolved and result.keep_state == "degraded"


def test_public_kept_txid_routes_to_retained_core(http_client, tmp_path, monkeypatch):
    txid = "D" * 43
    monkeypatch.setenv("RESOLVER_ARWEAVE_RETAINED_INTERNAL", "http://retained.internal")
    monkeypatch.setenv("RESOLVER_ARWEAVE_INTERNAL", "http://ordinary.internal")
    monkeypatch.setenv("RESOLVER_ARWEAVE_RETENTION_DB", str(tmp_path / "r.sqlite3"))
    get_settings.cache_clear()

    async def mark_kept():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={"x-cache": "HIT"}, content=b"x"))) as client:
            await keep_arweave(txid, "", get_settings(), client)

    asyncio.run(mark_kept())
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(200, headers={"content-type": "image/png", "x-cache": "HIT"}, stream=httpx.ByteStream(b"original"))

    original = app_module.app.state.client
    app_module.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        response = http_client.get(f"/arweave/{txid}")
    finally:
        app_module.app.state.client = original
        get_settings.cache_clear()
    assert response.status_code == 200 and response.content == b"original"
    assert seen == ["retained.internal"]

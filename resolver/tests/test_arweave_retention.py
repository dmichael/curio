import asyncio
import gzip
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


async def test_retained_keep_is_idempotent_only_after_exact_native_hit(tmp_path):
    settings = Settings(
        arweave_retained_internal="http://retained.internal",
        arweave_retention_db=str(tmp_path / "r.sqlite3"),
    )
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, headers={"x-cache": "HIT"}, content=b"kept")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await keep_arweave("I" * 43, "/exact-path", settings, client) == "kept"
        before = retained_records(settings)[0]
        assert await keep_arweave("I" * 43, "/exact-path", settings, client) == "kept"
        after = retained_records(settings)[0]

    # The second operation proves availability with HEAD; it never resets
    # intent or repeats the two hydration reads.
    assert calls == ["GET", "GET", "HEAD"]
    assert after["requested_at"] == before["requested_at"]


async def test_retained_keep_retries_degraded_hit_and_records_failure(tmp_path):
    settings = Settings(
        arweave_retained_internal="http://retained.internal",
        arweave_retention_db=str(tmp_path / "r.sqlite3"),
    )
    phase = "initial"

    def handler(request: httpx.Request) -> httpx.Response:
        if phase == "initial":
            return httpx.Response(200, headers={"x-cache": "HIT"}, content=b"kept")
        if request.method == "HEAD":
            return httpx.Response(200, headers={"x-cache": "MISS"})
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await keep_arweave("J" * 43, "", settings, client) == "kept"
        phase = "degraded"
        assert await keep_arweave("J" * 43, "", settings, client) == "failed"

    assert retained_records(settings)[0]["state"] == "failed"


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


def test_cold_ordinary_core_proxy_uses_cold_timeout_not_envoy_504(http_client, tmp_path, monkeypatch):
    txid = "O" * 43
    monkeypatch.setenv("RESOLVER_ARWEAVE_INTERNAL", "http://ar-io-core:4000")
    monkeypatch.setenv("RESOLVER_ARWEAVE_RETAINED_INTERNAL", "http://ar-io-retained:4000")
    monkeypatch.setenv("RESOLVER_ARWEAVE_RETENTION_DB", str(tmp_path / "r.sqlite3"))
    monkeypatch.setenv("RESOLVER_HTTP_TIMEOUT", "0.01")
    monkeypatch.setenv("RESOLVER_ARWEAVE_COLD_TIMEOUT", "1")
    get_settings.cache_clear()
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.extensions["timeout"]["read"]))
        if request.url.host == "ar-io-envoy":
            return httpx.Response(504)
        assert request.url.host == "ar-io-core"
        # MockTransport does not enforce timeouts; this models a Core cold read
        # that exceeds the generic HTTP budget and checks the explicit one.
        await asyncio.sleep(0.02)
        return httpx.Response(
            200, headers={"content-type": "video/mp4"}, stream=httpx.ByteStream(b"cold")
        )

    original = app_module.app.state.client
    app_module.app.state.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=0.01
    )
    try:
        response = http_client.get(f"/arweave/{txid}")
    finally:
        app_module.app.state.client = original
        get_settings.cache_clear()
    assert response.status_code == 200 and response.content == b"cold"
    assert seen == [("ar-io-core", 1.0)]


def test_kept_public_proxy_rejects_retained_miss_and_error(http_client, tmp_path, monkeypatch):
    txid = "M" * 43
    monkeypatch.setenv("RESOLVER_ARWEAVE_RETAINED_INTERNAL", "http://retained.internal")
    monkeypatch.setenv("RESOLVER_ARWEAVE_RETENTION_DB", str(tmp_path / "r.sqlite3"))
    get_settings.cache_clear()

    async def mark_kept():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"x-cache": "HIT"}, content=b"x")
        )) as client:
            await keep_arweave(txid, "", get_settings(), client)

    asyncio.run(mark_kept())
    original = app_module.app.state.client
    try:
        for response in (httpx.Response(200, headers={"x-cache": "MISS"}, content=b"ordinary"), httpx.Response(503)):
            app_module.app.state.client = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _, result=response: result)
            )
            result = http_client.get(f"/arweave/{txid}")
            assert result.status_code == 503
            assert result.json()["error"] == "retained AR.IO plane degraded"
    finally:
        app_module.app.state.client = original
        get_settings.cache_clear()


def test_native_proxy_preserves_raw_encoding_and_maps_connect_failure(http_client, monkeypatch):
    original = app_module.app.state.client
    seen = {}

    raw = gzip.compress(b"uncompressed media")

    def encoded(request: httpx.Request) -> httpx.Response:
        seen["accept_encoding"] = request.headers["accept-encoding"]
        return httpx.Response(
            200, headers={"content-type": "image/png", "content-encoding": "gzip", "vary": "Accept-Encoding"},
            stream=httpx.ByteStream(raw),
        )

    try:
        app_module.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(encoded))
        response = http_client.get("/ipfs/bafyCID/a.png")
        # TestClient decodes the valid wire bytes; receiving the original
        # payload proves the proxy did not corrupt the encoded stream.
        assert response.content == b"uncompressed media"
        assert response.headers["content-encoding"] == "gzip"
        assert response.headers["vary"] == "Accept-Encoding"
        assert seen["accept_encoding"] == "identity"

        async def unavailable(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        app_module.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(unavailable))
        response = http_client.get("/ipfs/bafyCID/a.png")
        assert response.status_code == 502 and response.json()["error"] == "native backend unavailable"
    finally:
        app_module.app.state.client = original


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

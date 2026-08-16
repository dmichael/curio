from urllib.parse import parse_qs, urlsplit

import httpx

from resolver import operations
from resolver.config import Settings, get_settings
from resolver.resolve import Resolved
from resolver.static_store import ResolutionStatus, StaticStore

_FORM_HEADERS = {"Origin": "http://testserver"}


def _redirect_target(response) -> str:
    assert response.status_code == 303
    location = response.headers["location"]
    assert urlsplit(location).path == "/display"
    return parse_qs(urlsplit(location).query)["uri"][0]


def test_homepage_is_a_simple_preview_first_form(http_client):
    response = http_client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<h1>Curio</h1>" in response.text
    assert 'method="post" action="/display"' in response.text
    assert 'name="uri" type="url"' in response.text
    assert 'name="save" type="checkbox"' in response.text
    assert "checked" not in response.text
    assert ">Resolve</button>" in response.text
    assert 'href="/healthz"' not in response.text
    assert "Digital artwork appliance" not in response.text
    assert "Version " in response.text
    assert 'href="/docs"' in response.text
    assert 'href="https://github.com/dmichael/curio"' in response.text
    assert '<svg aria-hidden="true"' in response.text
    assert 'href="/web/curio.css?v=' in response.text


def test_display_form_requires_same_origin(http_client, monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("cross-origin form must not reach resolution")

    monkeypatch.setattr(operations, "preview_reference", forbidden)
    for headers in (
        {},
        {"Origin": "https://attacker.example"},
        {"Sec-Fetch-Site": "cross-site"},
    ):
        response = http_client.post(
            "/display", data={"uri": "https://example.com/art.png"}, headers=headers
        )
        assert response.status_code == 403


def test_display_form_uses_browser_origin_when_media_origin_is_configured(
    http_client, monkeypatch
):
    monkeypatch.setenv("RESOLVER_PUBLIC_BASE_URL", "http://192.168.1.132:8090")
    get_settings.cache_clear()
    calls = []

    async def preview(ref, _settings, _client, origin):
        calls.append((ref, origin))
        return Resolved(
            ref, f"{origin}/ipfs/bafyPREVIEW/art.png", "play", "ipfs", True,
            content_type="image/png", source_kind="ipfs",
        )

    monkeypatch.setattr(operations, "preview_reference", preview)
    response = http_client.post(
        "/display",
        data={"uri": "ipfs://bafyPREVIEW/art.png"},
        headers={"Host": "siskin.local:8090", "Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert calls == [
        ("ipfs://bafyPREVIEW/art.png", "http://192.168.1.132:8090")
    ]
    get_settings.cache_clear()


def test_preview_resolves_once_without_storing(http_client, monkeypatch):
    calls = []

    async def preview(ref, _settings, _client, origin):
        calls.append((ref, origin))
        return Resolved(
            ref,
            f"{origin}/ipfs/bafyPREVIEW/art.png",
            "play",
            "ipfs",
            True,
            content_type="image/png",
            source_kind="ipfs",
            final_ref="ipfs://bafyPREVIEW/art.png",
        )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("preview must not use the durable store workflow")

    monkeypatch.setattr(operations, "preview_reference", preview)
    monkeypatch.setattr(operations, "store_reference", forbidden)
    response = http_client.post(
        "/display", data={"uri": "ipfs://bafyPREVIEW/art.png"},
        headers=_FORM_HEADERS, follow_redirects=False
    )
    assert calls == [("ipfs://bafyPREVIEW/art.png", "http://testserver")]
    assert _redirect_target(response) == "http://testserver/ipfs/bafyPREVIEW/art.png"


async def test_preview_operation_creates_only_evictable_static_media(tmp_path):
    settings = Settings(
        static_root=str(tmp_path / "media"),
        static_cache_max_bytes=1024,
        ssrf_dns_check=False,
    )

    def handler(_request):
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"preview")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await operations.preview_reference(
            "https://example.com/art.png", settings, client, "http://curio"
        )

    assert result.resolved
    media_id = result.resolved_url.rsplit("/", 1)[-1]
    store = StaticStore(settings.static_root, settings.static_cache_max_bytes)
    assert store.get(media_id)[0]["storage_status"] == "cached"
    assert store.resolution("https://example.com/art.png") is None


def test_save_uses_durable_workflow_once(http_client, monkeypatch):
    calls = []

    async def stored(ref, _settings, _client, origin):
        calls.append((ref, origin))
        return {
            "ref": ref,
            "media_url": f"{origin}/resolve?ref=ipfs%3A%2F%2FbafySAVE%2Fart.png",
            "status": "ready",
        }, True

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("saved web resolution must not preview first")

    monkeypatch.setattr(operations, "store_reference", stored)
    monkeypatch.setattr(operations, "preview_reference", forbidden)
    response = http_client.post(
        "/display",
        data={"uri": "ipfs://bafySAVE/art.png", "save": "on"},
        headers=_FORM_HEADERS,
        follow_redirects=False,
    )
    assert calls == [("ipfs://bafySAVE/art.png", "http://testserver")]
    assert _redirect_target(response) == (
        "http://testserver/resolve?ref=ipfs%3A%2F%2FbafySAVE%2Fart.png"
    )


def test_preview_failure_is_escaped_and_does_not_redirect(http_client, monkeypatch):
    async def failed(ref, *_args, **_kwargs):
        return Resolved(ref, ref, "play", None, False, note='<script id="injected">bad</script>')

    monkeypatch.setattr(operations, "preview_reference", failed)
    response = http_client.post(
        "/display", data={"uri": "https://dead.example/art"}, headers=_FORM_HEADERS
    )
    assert response.status_code == 422
    assert '<script id="injected">' not in response.text
    assert "&lt;script id=&quot;injected&quot;&gt;" in response.text
    assert 'value="https://dead.example/art"' in response.text


def test_display_accepts_available_same_origin_media(http_client, tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()
    store = StaticStore(str(tmp_path / "media"))
    entry = store.put(
        b"synthetic image",
        media_type="image/png",
        filename="fixture.png",
        source_ref="https://fixture.example/image.png",
    )
    uri = f"http://testserver/media/{entry['id']}.png"

    response = http_client.get("/display", params={"uri": uri})
    assert response.status_code == 200
    assert 'class="display"' in response.text
    assert f'data-media-uri="{uri}"' in response.text
    assert '<script src="/web/display.js?v=' in response.text
    assert "default-src 'none'" in response.headers["content-security-policy"]

    head = http_client.head(f"/media/{entry['id']}.png")
    assert head.status_code == 200
    assert head.headers["content-type"] == "image/png"
    assert head.content == b""
    get_settings.cache_clear()


def test_display_accepts_a_known_saved_resolve_uri(http_client, tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()
    store = StaticStore(str(tmp_path / "media"))
    store.record_resolution(
        canonical_ref="ipfs://bafySAVED/art.png",
        ref="ipfs://bafySAVED/art.png",
        final_ref="ipfs://bafySAVED/art.png",
        media_path="/ipfs/bafySAVED/art.png",
        status=ResolutionStatus.READY,
        media_type="image/png",
    )
    uri = "http://testserver/resolve?ref=ipfs%3A%2F%2FbafySAVED%2Fart.png"
    response = http_client.get("/display", params={"uri": uri})
    assert response.status_code == 200

    head = http_client.head(
        "/resolve", params={"ref": "ipfs://bafySAVED/art.png"}, follow_redirects=False
    )
    assert head.status_code == 302
    assert head.headers["location"] == "/ipfs/bafySAVED/art.png"
    get_settings.cache_clear()


def test_display_rejects_external_unknown_and_missing_targets(http_client, tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()
    rejected = [
        "https://example.com/art.png",
        "http://user:password@testserver/media/abc",
        "http://testserver/wallet",
        "http://testserver/media/missing.png",
        "http://testserver/resolve?ref=ipfs%3A%2F%2FbafyUNKNOWN",
        "http://testserver/ipfs/%2e%2e/secret",
    ]
    for uri in rejected:
        response = http_client.get("/display", params={"uri": uri})
        assert response.status_code == 404, uri
        assert uri not in response.text
    get_settings.cache_clear()


def test_display_script_has_all_first_pass_renderers(http_client):
    response = http_client.get("/web/display.js")
    assert response.status_code == 200
    assert "method: 'HEAD'" in response.text
    for element in ("img", "video", "audio", "iframe"):
        assert f"createElement('{element}')" in response.text
    assert "allow-scripts" in response.text
    assert "allow-same-origin" not in response.text
    assert "object-fit: contain" in http_client.get("/web/curio.css").text

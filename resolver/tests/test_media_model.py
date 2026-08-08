"""Target media-model contracts, independent of real gateways."""
import httpx

from resolver.config import Settings
from resolver.resolve import resolve_ref
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

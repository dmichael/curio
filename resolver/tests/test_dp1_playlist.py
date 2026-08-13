"""dp1_playlist: the unsigned DP-1 1.0.0 export shared by REST and MCP."""

import json
import re
import uuid

import pytest

from resolver import operations
from resolver.config import Settings, get_settings
from resolver.mcp_server import mcp
from resolver.static_store import ResolutionStatus, StaticStore

_CREATED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _store(tmp_path) -> StaticStore:
    return StaticStore(str(tmp_path / "media"))


def _settings(tmp_path) -> Settings:
    return Settings(static_root=str(tmp_path / "media"), ssrf_dns_check=False)


def _record_video(store: StaticStore, ref: str = "ipfs://bafyVIDEO/art.mp4") -> None:
    store.record_resolution(
        canonical_ref=ref,
        ref=ref,
        final_ref=ref,
        media_path="/ipfs/bafyVIDEO/art.mp4",
        status=ResolutionStatus.READY,
        media_type="video/mp4",
    )


def _record_image(store: StaticStore, ref: str = "ipfs://bafyIMAGE/art.png") -> None:
    store.record_resolution(
        canonical_ref=ref,
        ref=ref,
        final_ref=ref,
        media_path="/ipfs/bafyIMAGE/art.png",
        status=ResolutionStatus.READY,
        media_type="image/png",
    )


def _record_failed(store: StaticStore, ref: str = "ipfs://bafyDEAD/art.png") -> None:
    store.record_resolution(
        canonical_ref=ref,
        ref=ref,
        final_ref=ref,
        media_path="/ipfs/bafyDEAD/art.png",
        status=ResolutionStatus.FAILED,
        reason="providers gone",
    )


def _is_uuid4(value: str) -> bool:
    parsed = uuid.UUID(value)
    return parsed.version == 4


def test_video_item_loops_with_default_duration(tmp_path):
    store = _store(tmp_path)
    _record_video(store)
    settings = _settings(tmp_path)

    playlist = operations.dp1_playlist(
        ["ipfs://bafyVIDEO/art.mp4"], settings, "http://curio.example"
    )

    assert len(playlist["items"]) == 1
    item = playlist["items"][0]
    assert item["display"] == {"loop": True}
    assert item["duration"] == 86400
    assert item["source"] == "http://curio.example/ipfs/bafyVIDEO/art.mp4"
    assert item["license"] == "open"
    assert _is_uuid4(item["id"])


def test_image_item_has_no_display_object(tmp_path):
    store = _store(tmp_path)
    _record_image(store)
    settings = _settings(tmp_path)

    playlist = operations.dp1_playlist(
        ["ipfs://bafyIMAGE/art.png"], settings, "http://curio.example"
    )

    item = playlist["items"][0]
    assert "display" not in item
    assert item["duration"] == 86400
    assert item["source"] == "http://curio.example/ipfs/bafyIMAGE/art.png"


def test_caller_duration_and_title_are_respected_and_slug_is_derived(tmp_path):
    store = _store(tmp_path)
    _record_video(store)
    settings = _settings(tmp_path)

    playlist = operations.dp1_playlist(
        ["ipfs://bafyVIDEO/art.mp4"],
        settings,
        "http://curio.example",
        title="My Show, Volume 1!",
        duration=30,
    )

    assert playlist["title"] == "My Show, Volume 1!"
    assert playlist["slug"] == "my-show-volume-1"
    assert playlist["items"][0]["duration"] == 30


def test_default_title_and_slug(tmp_path):
    store = _store(tmp_path)
    _record_video(store)
    settings = _settings(tmp_path)

    playlist = operations.dp1_playlist(
        ["ipfs://bafyVIDEO/art.mp4"], settings, "http://curio.example"
    )

    assert playlist["title"] == "Curio playlist"
    assert playlist["slug"] == "curio-playlist"


def test_playlist_shape_matches_dp1(tmp_path):
    store = _store(tmp_path)
    _record_video(store)
    settings = _settings(tmp_path)

    playlist = operations.dp1_playlist(
        ["ipfs://bafyVIDEO/art.mp4"], settings, "http://curio.example"
    )

    assert playlist["dpVersion"] == "1.0.0"
    assert _is_uuid4(playlist["id"])
    assert _CREATED_RE.match(playlist["created"])
    assert len(playlist["slug"]) <= 64


def test_unknown_ref_raises_naming_it(tmp_path):
    settings = _settings(tmp_path)
    with pytest.raises(ValueError, match=re.escape("ipfs://bafyUNKNOWN/art.png")):
        operations.dp1_playlist(
            ["ipfs://bafyUNKNOWN/art.png"], settings, "http://curio.example"
        )


def test_failed_ref_raises_naming_it(tmp_path):
    store = _store(tmp_path)
    _record_failed(store)
    settings = _settings(tmp_path)
    with pytest.raises(ValueError, match=re.escape("ipfs://bafyDEAD/art.png")):
        operations.dp1_playlist(
            ["ipfs://bafyDEAD/art.png"], settings, "http://curio.example"
        )


def test_rest_returns_playlist(http_client, tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()
    _record_video(_store(tmp_path))

    response = http_client.post(
        "/playlist/dp1",
        json={"refs": ["ipfs://bafyVIDEO/art.mp4"]},
        headers={"Host": "curio.example"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["source"] == "http://curio.example/ipfs/bafyVIDEO/art.mp4"
    assert body["items"][0]["display"] == {"loop": True}
    get_settings.cache_clear()


def test_rest_unknown_ref_is_422(http_client, tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()

    response = http_client.post(
        "/playlist/dp1", json={"refs": ["ipfs://bafyUNKNOWN/art.png"]}
    )

    assert response.status_code == 422
    assert "ipfs://bafyUNKNOWN/art.png" in response.json()["error"]
    get_settings.cache_clear()


def test_rest_failed_ref_is_422(http_client, tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()
    _record_failed(_store(tmp_path))

    response = http_client.post(
        "/playlist/dp1", json={"refs": ["ipfs://bafyDEAD/art.png"]}
    )

    assert response.status_code == 422
    assert "ipfs://bafyDEAD/art.png" in response.json()["error"]
    get_settings.cache_clear()


def test_rest_rejects_empty_refs(http_client):
    response = http_client.post("/playlist/dp1", json={"refs": []})
    assert response.status_code == 422


def test_rest_rejects_non_positive_duration(http_client, tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()
    _record_video(_store(tmp_path))

    response = http_client.post(
        "/playlist/dp1",
        json={"refs": ["ipfs://bafyVIDEO/art.mp4"], "duration": 0},
    )

    assert response.status_code == 422
    get_settings.cache_clear()


async def test_mcp_tool_returns_playlist(tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()
    _record_video(_store(tmp_path))

    content, _ = await mcp.call_tool(
        "dp1_playlist", {"refs": ["ipfs://bafyVIDEO/art.mp4"]}
    )
    playlist = json.loads(content[0].text)

    assert playlist["items"][0]["display"] == {"loop": True}
    get_settings.cache_clear()


async def test_mcp_tool_raises_on_unknown_ref(tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()

    try:
        with pytest.raises(Exception, match=re.escape("ipfs://bafyUNKNOWN/art.png")):
            await mcp.call_tool("dp1_playlist", {"refs": ["ipfs://bafyUNKNOWN/art.png"]})
    finally:
        get_settings.cache_clear()

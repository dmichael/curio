"""Favorites: the household's owner-picked refs, keyed by canonical ref.

Store unit tests plus route-level CRUD. Route tests share the session-scoped
TestClient from conftest (the MCP session manager in the lifespan is
start-once-per-process); settings vary per test via env + lru_cache clearing.
"""

import json
import os

import httpx
import pytest

from resolver.config import Settings, get_settings
from resolver.favorites import (
    DuplicateFavorite,
    FavoriteNotFound,
    Favorites,
    FavoritesUnparseable,
    get_favorites,
)
from resolver.library import pin_resolved
from resolver.resolve import Resolved

# --- store ---------------------------------------------------------------


def test_add_list_remove_round_trip(tmp_path):
    path = tmp_path / "missing" / "favorites.json"  # parent dir created on write
    favorites = Favorites(str(path))
    record = favorites.add("  ipfs://bafyFAV/art.png ", title="Fav #1", note="living room")
    assert record["ref"] == "ipfs://bafyFAV/art.png"  # stored stripped
    assert record["key"] == "ipfs://bafyFAV/art.png"
    assert record["added_at"].startswith("20")

    # write-through: visible with no mtime poke, in the same second
    assert [r["title"] for r in favorites.list_favorites()] == ["Fav #1"]

    # the file itself is valid JSON after every write
    favorites.add("ar://TX123/piece", note='has "quotes" and émoji 🎨')
    on_disk = json.loads(path.read_text())
    assert [r["ref"] for r in on_disk] == ["ipfs://bafyFAV/art.png", "ar://TX123/piece"]
    assert on_disk[1]["note"] == 'has "quotes" and émoji 🎨'

    # removal matches any spelling of the same content
    removed = favorites.remove("https://ipfs.io/ipfs/bafyFAV/art.png")
    assert removed["ref"] == "ipfs://bafyFAV/art.png"
    assert [r["ref"] for r in favorites.list_favorites()] == ["ar://TX123/piece"]
    with pytest.raises(FavoriteNotFound):
        favorites.remove("ipfs://bafyFAV/art.png")


def test_duplicate_matches_any_spelling(tmp_path):
    favorites = Favorites(str(tmp_path / "favorites.json"))
    favorites.add("ipfs://bafyFAV/art.png")
    with pytest.raises(DuplicateFavorite, match="already a favorite"):
        favorites.add("/ipfs/bafyFAV/art.png")  # same content, respelled


def test_mutation_refuses_to_rewrite_a_broken_file(tmp_path):
    path = tmp_path / "favorites.json"
    broken = "[{not json"
    path.write_text(broken)
    favorites = Favorites(str(path))
    with pytest.raises(FavoritesUnparseable):
        favorites.add("ipfs://bafyFAV/art.png")
    with pytest.raises(FavoritesUnparseable):
        favorites.remove("ipfs://bafyFAV/art.png")
    assert path.read_text() == broken  # untouched, byte for byte

    # parseable-but-wrong shapes also block writes
    path.write_text('{"ref": "not a list"}')
    with pytest.raises(FavoritesUnparseable, match="not a JSON list"):
        favorites.add("ipfs://bafyFAV/art.png")


def test_broken_edit_keeps_the_previous_table(tmp_path):
    path = tmp_path / "favorites.json"
    favorites = Favorites(str(path))
    favorites.add("ipfs://bafyFAV/art.png")

    path.write_text("[{not json")
    stat = os.stat(path)
    os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))

    assert [r["ref"] for r in favorites.list_favorites()] == ["ipfs://bafyFAV/art.png"]


def test_hand_edit_reloads_on_mtime_change(tmp_path):
    path = tmp_path / "favorites.json"
    favorites = Favorites(str(path))
    favorites.add("ipfs://bafyFAV/art.png")

    path.write_text(json.dumps([{"ref": "ar://TXHAND/x"}]))
    stat = os.stat(path)
    os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))

    assert [r["key"] for r in favorites.list_favorites()] == ["ar://TXHAND/x"]


def test_missing_file_is_empty(tmp_path):
    favorites = Favorites(str(tmp_path / "does-not-exist.json"))
    assert favorites.list_favorites() == []


# --- pinning: a favorite is a keep-this signal ----------------------------


PIN_SETTINGS = Settings(ipfs_api="http://kubo.internal", arweave_internal="http://ar.internal")


async def test_pin_resolved_pins_ipfs_target(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, dict(request.url.params)))
        return httpx.Response(200, json={"Pins": ["bafyFAV"]})

    result = Resolved(
        "ipfs://bafyFAV/art.png",
        "http://box:8080/ipfs/bafyFAV/art.png?filename=art.png",
        "play", "ipfs", True,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await pin_resolved(result, PIN_SETTINGS, client)
    assert outcome == "pinned"
    # Pin the canonical root; the resolved path remains serving/provenance.
    assert calls == [("/api/v0/pin/add", {"arg": "/ipfs/bafyFAV"})]


async def test_pin_resolved_warms_arweave_and_skips_unresolved(tmp_path):
    settings = PIN_SETTINGS.model_copy(update={"arweave_retention_db": str(tmp_path / "retained.sqlite3")})
    warmed = []

    def handler(request: httpx.Request) -> httpx.Response:
        warmed.append(str(request.url))
        return httpx.Response(200, headers={"x-cache": "HIT"}, content=b"bytes")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        arweave = Resolved("ar://TX123", "http://box:3000/TX123", "play", "arweave", True)
        assert await pin_resolved(arweave, settings, client) == "kept"
        assert warmed == ["http://127.0.0.1:4001/TX123", "http://127.0.0.1:4001/TX123"]

        dead = Resolved("ipfs://bafyDEAD", "ipfs://bafyDEAD", "play", "ipfs", False)
        assert await pin_resolved(dead, settings, client) is None  # nothing fetched


# --- pin option on /resolve ------------------------------------------------


def test_resolve_pin_option_schedules_and_default_does_not(http_client):
    # default: pure resolution, no pin key at all
    plain = http_client.get("/resolve", params={"ref": "ipfs://bafyCID/art.png"}).json()
    assert "pin_scheduled" not in plain

    pinned = http_client.get(
        "/resolve", params={"ref": "ipfs://bafyCID/art.png", "pin": 1}
    ).json()
    assert pinned["pin_scheduled"] is False  # unavailable native backend is not an optimistic pin

    unresolvable = http_client.get(
        "/resolve", params={"ref": "not a reference", "pin": 1}
    ).json()
    assert unresolvable["pin_scheduled"] is False  # nothing to keep


# --- routes ---------------------------------------------------------------


@pytest.fixture
def client(http_client, tmp_path, monkeypatch):
    """The shared client, pointed at a tmp favorites file. Teardown clears the
    caches again; the monkeypatch env restore lands after, so the next
    get_settings() rebuilds from the clean environment."""
    monkeypatch.setenv("RESOLVER_FAVORITES_PATH", str(tmp_path / "favorites.json"))
    monkeypatch.setenv("RESOLVER_IPFS_PUBLIC_BASE", "http://box:8080")
    get_settings.cache_clear()
    get_favorites.cache_clear()
    yield http_client
    get_settings.cache_clear()
    get_favorites.cache_clear()


def test_favorite_crud_round_trip(client):
    created = client.post(
        "/favorites", params={"ref": "ipfs://bafyCID/art.png", "note": "hall screen"}
    )
    assert created.status_code == 201
    body = created.json()
    assert body["key"] == "ipfs://bafyCID/art.png"
    assert body["note"] == "hall screen"
    # An unavailable Kubo artifact is not advertised as renderer-ready.
    assert body["resolved"] is False
    assert body["resolved_url"] is None
    assert body["final_ref"] == "ipfs://bafyCID/art.png"
    assert body["source_ref"] == "ipfs://bafyCID/art.png"
    assert body["title"] is None
    assert body["pin_scheduled"] is False

    # The list retains the discovery record but exposes the unavailable state.
    listed = client.get("/favorites").json()
    assert listed["count"] == 1
    entry = listed["favorites"][0]
    assert entry["ref"] == "ipfs://bafyCID/art.png"
    assert entry["resolved"] is False
    assert entry["resolved_url"] is None
    assert entry["playback_method"] == "play"

    removed = client.request(
        "DELETE", "/favorites", params={"ref": "https://ipfs.io/ipfs/bafyCID/art.png"}
    )
    assert removed.status_code == 200
    assert removed.json()["removed"]["ref"] == "ipfs://bafyCID/art.png"
    assert client.get("/favorites").json()["count"] == 0


def test_favorite_duplicate_conflicts(client):
    assert client.post("/favorites", params={"ref": "ipfs://bafyCID/art.png"}).status_code == 201
    dup = client.post("/favorites", params={"ref": "/ipfs/bafyCID/art.png"})
    assert dup.status_code == 409
    assert "already a favorite" in dup.json()["error"]
    assert client.get("/favorites").json()["count"] == 1


def test_favorite_delete_absent_is_404(client):
    gone = client.request("DELETE", "/favorites", params={"ref": "ipfs://bafyNOPE/x"})
    assert gone.status_code == 404
    assert "no favorite" in gone.json()["error"]


def test_favorite_endpoints_disabled_without_path(http_client, monkeypatch):
    monkeypatch.delenv("RESOLVER_FAVORITES_PATH", raising=False)
    get_settings.cache_clear()
    try:
        responses = (
            http_client.get("/favorites"),
            http_client.post("/favorites", params={"ref": "ipfs://bafyCID/x"}),
            http_client.request("DELETE", "/favorites", params={"ref": "x"}),
        )
        for response in responses:
            assert response.status_code == 503
            assert "RESOLVER_FAVORITES_PATH" in response.json()["error"]
    finally:
        get_settings.cache_clear()

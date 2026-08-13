"""POST records storage intent; GET only plays known references."""

import sqlite3
from urllib.parse import parse_qs, urlsplit

import httpx

from resolver import operations
from resolver.config import Settings, get_settings
from resolver.resolve import Resolved, resolve_ref
from resolver.static_store import ResolutionStatus, StaticStore


def _media_ref(url: str) -> str:
    return parse_qs(urlsplit(url).query)["ref"][0]


def test_unknown_get_is_404_without_resolving(http_client, tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("GET must not resolve unknown references")

    monkeypatch.setattr(operations, "resolve_ref", forbidden)
    response = http_client.get("/resolve", params={"ref": "ipfs://bafyUNKNOWN/art.png"})
    assert response.status_code == 404
    get_settings.cache_clear()


def test_post_stores_ipfs_and_get_redirects_any_equivalent_spelling(
    http_client, tmp_path, monkeypatch
):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()
    pinned = []

    async def resolved(ref, *_args, **_kwargs):
        return Resolved(
            ref,
            "http://curio.example/ipfs/bafyART/art.mp4?filename=art.mp4",
            "play",
            "ipfs",
            True,
            content_type="video/mp4",
            source_kind="ipfs",
            final_ref="ipfs://bafyART/art.mp4",
        )

    async def pin(result, *_args, **_kwargs):
        pinned.append(result.final_ref)
        return "pinned"

    monkeypatch.setattr(operations, "resolve_ref", resolved)
    monkeypatch.setattr(operations, "store_resolved", pin)
    response = http_client.post(
        "/resolve",
        params={"ref": "https://ipfs.io/ipfs/bafyART/art.mp4?download=1"},
        headers={"Host": "curio.example"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ref": "https://ipfs.io/ipfs/bafyART/art.mp4?download=1",
        "final_ref": "ipfs://bafyART/art.mp4",
        "media_url": (
            "http://curio.example/resolve?"
            "ref=https%3A%2F%2Fipfs.io%2Fipfs%2FbafyART%2Fart.mp4%3Fdownload%3D1"
        ),
        "status": ResolutionStatus.READY.value,
        "media_type": "video/mp4",
        "source_kind": "ipfs",
        "playback_method": "play",
    }
    assert pinned == ["ipfs://bafyART/art.mp4"]

    playback = http_client.get(
        "/resolve", params={"ref": "ipfs://bafyART/art.mp4"}, follow_redirects=False
    )
    assert playback.status_code == 302
    assert playback.headers["location"] == "/ipfs/bafyART/art.mp4?filename=art.mp4"
    get_settings.cache_clear()


def test_json_post_accepts_reference(http_client, tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()

    async def unresolved(ref, *_args, **_kwargs):
        return Resolved(ref, ref, "play", None, False, note="fixture")

    monkeypatch.setattr(operations, "resolve_ref", unresolved)
    response = http_client.post(
        "/resolve", json={"ref": "https://example.com/token.json"}
    )
    assert response.status_code == 422
    assert response.json()["ref"] == "https://example.com/token.json"
    get_settings.cache_clear()


def test_failed_post_is_not_registered(http_client, tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()

    async def unresolved(ref, *_args, **_kwargs):
        return Resolved(ref, ref, "play", None, False, note="not found")

    monkeypatch.setattr(operations, "resolve_ref", unresolved)
    response = http_client.post("/resolve", params={"ref": "https://dead.example/art"})
    assert response.status_code == 422
    assert response.json() == {
        "ref": "https://dead.example/art",
        "status": ResolutionStatus.FAILED.value,
        "reason": "not found",
    }
    assert http_client.get(
        "/resolve", params={"ref": "https://dead.example/art"}
    ).status_code == 404
    get_settings.cache_clear()


def test_live_dependent_shell_is_stored_and_disclosed(http_client, tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()

    async def resolved(ref, *_args, **_kwargs):
        return Resolved(
            ref,
            "http://testserver/ipfs/bafyRUNTIME/index.html",
            "send",
            "ipfs",
            True,
            content_type="text/html",
            source_kind="ipfs",
            final_ref="ipfs://bafyRUNTIME/index.html",
            status=ResolutionStatus.LIVE_DEPENDENT,
        )

    async def pin(*_args, **_kwargs):
        return "pinned"

    monkeypatch.setattr(operations, "resolve_ref", resolved)
    monkeypatch.setattr(operations, "store_resolved", pin)
    response = http_client.post("/resolve", params={"ref": "ipfs://bafyRUNTIME/index.html"})
    assert response.status_code == 200
    assert response.json()["status"] == ResolutionStatus.LIVE_DEPENDENT.value
    assert response.json()["playback_method"] == "send"
    get_settings.cache_clear()


def test_multipart_post_stores_upload_and_returned_ref_plays(http_client, tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()
    response = http_client.post(
        "/resolve", files={"file": ("piece.png", b"png-bytes", "image/png")}
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["ref"].startswith("upload:sha256:")
    assert payload["status"] == ResolutionStatus.READY.value
    assert payload["source_kind"] == "upload"
    assert "keep_state" not in payload

    playback = http_client.get(
        "/resolve", params={"ref": _media_ref(payload["media_url"])}, follow_redirects=False
    )
    assert playback.status_code == 302
    media = http_client.get(playback.headers["location"])
    assert media.content == b"png-bytes"
    assert media.headers["content-type"] == "image/png"
    get_settings.cache_clear()


def test_multipart_post_over_cap_is_413(http_client, tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("RESOLVER_STATIC_MAX_BYTES", "4")
    get_settings.cache_clear()
    response = http_client.post(
        "/resolve", files={"file": ("big.png", b"way-too-many-bytes", "image/png")}
    )
    assert response.status_code == 413
    assert response.json()["error"] == "body exceeds 4 bytes"
    get_settings.cache_clear()


async def test_storage_intent_bypasses_evictable_cache_quota(tmp_path):
    settings = Settings(
        static_root=str(tmp_path / "media"),
        static_cache_max_bytes=3,
        static_max_bytes=16,
        ssrf_dns_check=False,
    )

    def handler(_request):
        return httpx.Response(
            200, headers={"content-type": "image/png"}, content=b"four"
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        payload, stored = await operations.store_reference(
            "https://example.com/art.png", settings, client, "http://curio"
        )

    assert stored and payload["status"] == "ready"
    store = StaticStore(settings.static_root, settings.static_cache_max_bytes)
    record = store.resolution("https://example.com/art.png")
    # media_path now carries a minted display extension (e.g. ".png"); media
    # ids are dot-free uuid4 hex, so the id is everything before the first dot.
    media_id = str(record["media_path"]).rsplit("/", 1)[-1].split(".", 1)[0]
    assert store.get(media_id)[0]["storage_status"] == "stored"


def test_get_does_not_redirect_a_recorded_failure(http_client, tmp_path, monkeypatch):
    monkeypatch.setenv("RESOLVER_STATIC_ROOT", str(tmp_path / "media"))
    get_settings.cache_clear()
    StaticStore(str(tmp_path / "media")).record_resolution(
        canonical_ref="ipfs://bafyDEAD/art.png",
        ref="ipfs://bafyDEAD/art.png",
        final_ref="ipfs://bafyDEAD/art.png",
        media_path="/ipfs/bafyDEAD/art.png",
        status=ResolutionStatus.FAILED,
        reason="providers gone",
    )
    response = http_client.get(
        "/resolve", params={"ref": "ipfs://bafyDEAD/art.png"}, follow_redirects=False
    )
    assert response.status_code == 404
    assert response.json()["reason"] == "providers gone"
    get_settings.cache_clear()


async def test_resolve_ref_does_not_play_a_recorded_failure(tmp_path):
    # Every reader of the resolutions table must give the same answer as
    # GET /resolve: a FAILED record is not playable.
    settings = Settings(static_root=str(tmp_path / "media"), ssrf_dns_check=False)
    StaticStore(settings.static_root).record_resolution(
        canonical_ref="upload:sha256:deadbeef",
        ref="upload:sha256:deadbeef",
        final_ref="upload:sha256:deadbeef",
        media_path="/media/deadbeef",
        status=ResolutionStatus.FAILED,
        reason="stored bytes rejected",
    )
    async with httpx.AsyncClient() as client:
        result = await resolve_ref("upload:sha256:deadbeef", settings, client)
    assert result.resolved is False
    assert result.status == ResolutionStatus.FAILED
    assert result.resolved_url == "upload:sha256:deadbeef"
    assert result.note == "stored bytes rejected"


def test_resolution_schema_migrates_existing_static_database(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    path = root / "library.sqlite3"
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE media (
            id TEXT PRIMARY KEY, digest TEXT NOT NULL, filename TEXT,
            media_type TEXT, bytes INTEGER NOT NULL, keep_state TEXT NOT NULL,
            source_ref TEXT NOT NULL, retrieved_at TEXT NOT NULL,
            accessed_at TEXT NOT NULL
        )"""
    )
    db.execute("PRAGMA user_version = 1")
    db.commit()
    db.close()

    store = StaticStore(str(root))
    db = store._connection()
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        columns = {row["name"] for row in db.execute("PRAGMA table_info(resolutions)")}
    finally:
        db.close()
    assert columns == {
        "canonical_ref",
        "ref",
        "final_ref",
        "media_path",
        "status",
        "media_type",
        "reason",
        "created_at",
        "updated_at",
    }

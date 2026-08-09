"""Route-level tests for override CRUD and multipart /resolve.

All tests share the session-scoped TestClient from conftest (the MCP session
manager in the lifespan is start-once-per-process); settings vary per test
via env + lru_cache clearing.
"""

import tomllib

import pytest

from resolver import operations
from resolver.config import get_settings
from resolver.overrides import get_registry
from resolver.resolve import Resolved

ENTRY = {
    "ref": "ipfs://bafyDEAD/art.png",
    "replacement": "ipfs://bafyALT/master.png",
    "status": "alternate-master",
    "token": "eip155:1/erc721:0xabc/0",
}


@pytest.fixture
def client(http_client, tmp_path, monkeypatch):
    """The shared client, pointed at a tmp registry/capture dir. Teardown
    clears the caches again; the monkeypatch env restore lands after, so the
    next get_settings() rebuilds from the clean environment."""
    monkeypatch.setenv("RESOLVER_OVERRIDES_PATH", str(tmp_path / "overrides.toml"))
    monkeypatch.setenv("RESOLVER_SEED_CAPTURE_DIR", str(tmp_path / "captures"))
    monkeypatch.setenv("RESOLVER_IPFS_PUBLIC_BASE", "http://box:8080")
    get_settings.cache_clear()
    get_registry.cache_clear()
    yield http_client
    get_settings.cache_clear()
    get_registry.cache_clear()


def test_override_crud_round_trip(client, monkeypatch):
    created = client.post("/override", json=ENTRY)
    assert created.status_code == 201
    body = created.json()
    assert body["canonical_key"] == "ipfs://bafyDEAD/art.png"
    assert body["replaced"] is False
    # Disclosure reports actual local-backend availability, not URL shape.
    assert body["replacement_resolved"] is False
    assert body["replacement_resolved_url"] is None

    listed = client.get("/override").json()
    assert listed["count"] == 1
    assert listed["entries"][0]["ref"] == ENTRY["ref"]

    raw = client.get("/override", params={"raw": 1})
    assert raw.status_code == 200
    assert tomllib.loads(raw.text)["override"][0]["replacement"] == ENTRY["replacement"]

    # the registry is live immediately when the dead ref is stored.
    async def substituted(ref, *_args, **_kwargs):
        return Resolved(
            ref,
            "http://testserver/ipfs/bafyALT/master.png",
            "play",
            "ipfs",
            True,
            source_kind="ipfs",
            final_ref="ipfs://bafyALT/master.png",
            substituted=True,
            substituted_ref=ref,
            substitution_status="alternate-master",
        )

    async def pinned(*_args, **_kwargs):
        return "pinned"

    monkeypatch.setattr(operations, "resolve_ref", substituted)
    monkeypatch.setattr(operations, "store_resolved", pinned)
    resolved = client.post(
        "/resolve", params={"ref": "https://ipfs.io/ipfs/bafyDEAD/art.png"}
    ).json()
    assert resolved["substituted"] is True
    assert resolved["substitution_status"] == "alternate-master"
    playback = client.get(
        "/resolve",
        params={"ref": "ipfs://bafyDEAD/art.png"},
        follow_redirects=False,
    )
    assert playback.headers["location"] == "/ipfs/bafyALT/master.png"

    removed = client.request("DELETE", "/override", params={"ref": "/ipfs/bafyDEAD/art.png"})
    assert removed.status_code == 200
    assert removed.json()["removed"]["ref"] == ENTRY["ref"]
    assert client.get("/override").json()["count"] == 0


def test_override_duplicate_conflicts_unless_replace(client):
    assert client.post("/override", json=ENTRY).status_code == 201
    dup = {**ENTRY, "ref": "https://ipfs.io/ipfs/bafyDEAD/art.png"}  # same content, respelled
    conflict = client.post("/override", json=dup)
    assert conflict.status_code == 409
    assert "replace" in conflict.json()["error"]
    replaced = client.post("/override", json={**dup, "replace": True})
    assert replaced.status_code == 201
    assert replaced.json()["replaced"] is True
    assert client.get("/override").json()["count"] == 1


def test_override_bad_status_and_absent_ref(client):
    bad = client.post("/override", json={**ENTRY, "status": "definitely-not"})
    assert bad.status_code == 400
    assert "not one of" in bad.json()["error"]

    gone = client.request("DELETE", "/override", params={"ref": "ipfs://bafyNOPE/x"})
    assert gone.status_code == 404

    # nothing was ever written, so there is no file to snapshot
    assert client.get("/override", params={"raw": 1}).status_code == 404


def test_override_endpoints_disabled_without_path(http_client, monkeypatch):
    monkeypatch.delenv("RESOLVER_OVERRIDES_PATH", raising=False)
    get_settings.cache_clear()
    try:
        responses = (
            http_client.get("/override"),
            http_client.post("/override", json=ENTRY),
            http_client.request("DELETE", "/override", params={"ref": "x"}),
        )
        for response in responses:
            assert response.status_code == 503
            assert "RESOLVER_OVERRIDES_PATH" in response.json()["error"]
    finally:
        get_settings.cache_clear()


def test_resolve_route_uploads_and_records(client):
    response = client.post(
        "/resolve", files={"file": ("m.png", b"png-bytes", "image/png")}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["source_kind"] == "upload"
    assert body["status"] == "ready"
    assert body["ref"].startswith("upload:sha256:")
    assert body["media_url"].startswith("http://testserver/resolve?")
    assert body["integrity"]["algorithm"] == "sha256"


def test_upload_does_not_require_capture_dir(http_client, monkeypatch):
    monkeypatch.delenv("RESOLVER_SEED_CAPTURE_DIR", raising=False)
    get_settings.cache_clear()
    try:
        response = http_client.post(
            "/resolve", files={"file": ("m.png", b"x", "image/png")}
        )
        assert response.status_code == 201
        assert response.json()["source_kind"] == "upload"
    finally:
        get_settings.cache_clear()

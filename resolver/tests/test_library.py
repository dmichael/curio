"""Warm/capture ledgers and cross-plane library status (library.py, /library)."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from resolver import app as app_module
from resolver.config import Settings, get_settings
from resolver.favorites import get_favorites
from resolver.library import library_status, pin_resolved, record_warm, warmed_txids
from resolver.overrides import get_registry
from resolver.seed import SeedJob, _warm_txid, run_seed

SETTINGS = Settings(
    ipfs_internal="http://ipfs.internal",
    arweave_internal="http://ar.internal",
    ipfs_api="http://kubo.internal",
)


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def fake_net(routes: dict[str, dict | list]) -> tuple[httpx.AsyncClient, list[str]]:
    """Client over a routing table; also returns the request log.

    A list value serves its entries to successive requests (last one sticks),
    for endpoints whose behavior changes between calls.
    """
    log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(f"{request.method} {request.url}")
        spec = routes.get(f"{request.method} {request.url}") or routes.get(str(request.url))
        if isinstance(spec, list):
            spec = spec.pop(0) if len(spec) > 1 else spec[0]
        if spec is None:
            return httpx.Response(404)
        return httpx.Response(**spec)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), log


# --- warm ledger ------------------------------------------------------------


def test_record_warm_dedups_and_reads_back_in_order(tmp_path):
    settings = SETTINGS.model_copy(update={"seed_capture_dir": str(tmp_path)})
    record_warm("txA", settings, why="seed")
    record_warm("txB", settings, why="favorite")
    record_warm("txA", settings, why="resolve pin")  # already ledgered — dropped

    assert warmed_txids(settings) == ["txA", "txB"]
    records = [
        json.loads(line) for line in (tmp_path / "warmed.jsonl").read_text().splitlines()
    ]
    assert len(records) == 2
    assert records[0]["why"] == "seed"
    assert records[1]["why"] == "favorite"
    assert all(record["warmed_at"] for record in records)


def test_record_warm_noops_when_capture_disabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # a stray relative write would land here
    record_warm("txA", SETTINGS, why="seed")  # seed_capture_dir unset
    assert warmed_txids(SETTINGS) == []
    assert not list(tmp_path.iterdir())


async def test_warm_txid_records_success_not_failure(tmp_path):
    settings = SETTINGS.model_copy(update={"seed_capture_dir": str(tmp_path)})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/txGOOD":
            return httpx.Response(200, content=b"0" * 100)
        return httpx.Response(404)

    job = SeedJob(id="test1234", ref="0xAB", chain="ethereum", started_at="t")
    sem = asyncio.Semaphore(1)
    async with client_for(handler) as client:
        await _warm_txid("txGOOD", job, settings, client, sem)
        await _warm_txid("txGONE", job, settings, client, sem)

    assert job.warmed == 1
    assert job.failed == 1
    assert warmed_txids(settings) == ["txGOOD"]  # failures record nothing
    record = json.loads((tmp_path / "warmed.jsonl").read_text())
    assert record["why"] == "seed"


async def test_pin_resolved_arweave_warm_records_the_caller_why(tmp_path):
    settings = SETTINGS.model_copy(update={"seed_capture_dir": str(tmp_path)})
    result = SimpleNamespace(
        resolved=True,
        resolved_url="http://ar.internal/txFAV",
        provider="arweave",
        original_ref="ar://txFAV",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"art bytes")

    async with client_for(handler) as client:
        outcome = await pin_resolved(result, settings, client, why="favorite")

    assert outcome == "kept"
    assert warmed_txids(settings) == []  # retained-plane hydration is not an ordinary warm


# --- capture ledger (seed-driven) --------------------------------------------

SEED_SETTINGS = SETTINGS.model_copy(update={"blockscout_base": "http://bs.internal/api/v2"})

ETH_ADDR = "0xAbAbAbAbAbAbAbAbAbAbAbAbAbAbAbAbAbAbAbAb"
CAPTURE_URL = "https://hodlers.example/art/149.mp4"
CAPTURE_BYTES = b"the only copy of these bytes"


def make_job(ref: str, chain: str) -> SeedJob:
    # run_seed expects the address already resolved (start_seed's job);
    # these direct-run tests all use address-shaped refs.
    return SeedJob(id="test1234", ref=ref, chain=chain, address=ref, started_at="t")


def capture_routes():
    return {
        f"http://bs.internal/api/v2/addresses/{ETH_ADDR}/nft?type=ERC-721%2CERC-1155": {
            "status_code": 200,
            "json": {
                "items": [{"id": "149", "metadata": {"animation_url": CAPTURE_URL}}],
                "next_page_params": None,
            },
        },
        CAPTURE_URL: {
            "status_code": 200,
            "headers": {"content-type": "video/mp4"},
            "content": CAPTURE_BYTES,
        },
        "POST http://kubo.internal/api/v0/add?cid-version=1": {
            "status_code": 200,
            "json": {"Name": "captured", "Hash": "bafyCAPTURED", "Size": "28"},
        },
    }


async def test_http_only_media_is_captured_with_provenance(tmp_path):
    settings = SEED_SETTINGS.model_copy(update={"seed_capture_dir": str(tmp_path), "static_root": str(tmp_path / "media"), "ssrf_dns_check": False})
    client, _ = fake_net(capture_routes())
    job = make_job(ETH_ADDR, "ethereum")
    async with client:
        await run_seed(job, settings, client)
    assert job.status == "done", job.errors
    assert job.captured == 1
    assert job.skipped == 0 and job.failed == 0
    assert not (tmp_path / "captures.jsonl").exists()  # HTTP never enters Kubo


async def test_capture_is_once_per_url_across_jobs(tmp_path):
    settings = SEED_SETTINGS.model_copy(update={"seed_capture_dir": str(tmp_path), "static_root": str(tmp_path / "media"), "ssrf_dns_check": False})
    first, _ = fake_net(capture_routes())
    async with first:
        await run_seed(make_job(ETH_ADDR, "ethereum"), settings, first)

    second, log = fake_net(capture_routes())
    job = make_job(ETH_ADDR, "ethereum")
    async with second:
        await run_seed(job, settings, second)
    assert job.status == "done", job.errors
    assert job.captured == 1
    assert job.skipped == 0
    assert any(CAPTURE_URL in line for line in log)  # explicit seed retains HTTP statically


async def test_http_media_is_skipped_when_capture_is_off():
    settings = SEED_SETTINGS.model_copy(update={"static_root": "/tmp/curio-seed-test", "ssrf_dns_check": False})
    client, _ = fake_net(capture_routes())
    job = make_job(ETH_ADDR, "ethereum")
    async with client:
        await run_seed(job, settings, client)
    assert job.status == "done", job.errors
    assert job.captured == 1
    assert job.skipped == 0 and job.failed == 0


async def test_capture_failure_is_counted_not_fatal(tmp_path):
    settings = SEED_SETTINGS.model_copy(update={"seed_capture_dir": str(tmp_path), "static_root": str(tmp_path / "media"), "ssrf_dns_check": False})
    routes = capture_routes()
    routes[CAPTURE_URL] = {"status_code": 410}  # the domain died mid-seed
    client, _ = fake_net(routes)
    job = make_job(ETH_ADDR, "ethereum")
    async with client:
        await run_seed(job, settings, client)
    assert job.status == "done"
    assert job.captured == 0
    assert job.failed == 1 and job.skipped == 0
    assert not (tmp_path / "captures.jsonl").exists()


# --- library_status ----------------------------------------------------------


def kubo_handler(request: httpx.Request) -> httpx.Response | None:
    if request.url.path == "/api/v0/pin/ls":
        assert dict(request.url.params) == {"type": "recursive"}  # never type=all
        keys = {cid: {"Type": "recursive"} for cid in ("bafyA", "bafyB", "bafyC")}
        return httpx.Response(200, json={"Keys": keys})
    if request.url.path == "/api/v0/repo/stat":
        return httpx.Response(200, json={"RepoSize": 123_456, "NumObjects": 42})
    return None


async def test_library_status_counts_all_three_planes(tmp_path):
    settings = SETTINGS.model_copy(update={"seed_capture_dir": str(tmp_path)})
    record_warm("txHIT", settings, why="seed")
    record_warm("txEVICTED", settings, why="favorite")

    def handler(request: httpx.Request) -> httpx.Response:
        kubo = kubo_handler(request)
        if kubo is not None:
            return kubo
        assert request.method == "HEAD"
        cache = "HIT" if request.url.path == "/txHIT" else "MISS"
        return httpx.Response(200, headers={"x-cache": cache})

    async with client_for(handler) as client:
        status = await library_status(settings, client)

    assert status["ipfs"] == {"pinned": 3, "repo_size_bytes": 123_456, "repo_objects": 42}
    assert status["arweave"]["known_warmed"] == 2
    assert status["arweave"]["currently_cached"] == 1
    assert "evictable" in status["arweave"]["note"]  # eviction is visible, not silent
    # capture enabled but nothing captured yet; overrides/favorites disabled
    assert status["registry"] == {"overrides": None, "favorites": None, "captures": 0}


async def test_library_status_degrades_per_plane():
    # Kubo down, ledger disabled, no operator state: still a 3-section answer.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with client_for(handler) as client:
        status = await library_status(SETTINGS, client)

    assert "error" in status["ipfs"]
    assert set(status["arweave"]["retained"]) == {"kept", "pending", "failed", "operation"}
    assert "retained-plane" in status["arweave"]["retained"]["operation"]
    assert status["registry"] == {"overrides": None, "favorites": None, "captures": None}


# --- GET /library -------------------------------------------------------------


@pytest.fixture
def library_env(http_client, tmp_path, monkeypatch):
    """The shared client with every subsystem enabled against tmp paths."""
    monkeypatch.setenv("RESOLVER_SEED_CAPTURE_DIR", str(tmp_path))
    monkeypatch.setenv("RESOLVER_ARWEAVE_RETENTION_DB", str(tmp_path / "retained.sqlite3"))
    monkeypatch.setenv("RESOLVER_OVERRIDES_PATH", str(tmp_path / "overrides.toml"))
    monkeypatch.setenv("RESOLVER_FAVORITES_PATH", str(tmp_path / "favorites.json"))
    get_settings.cache_clear()
    get_registry.cache_clear()
    get_favorites.cache_clear()
    yield http_client
    get_settings.cache_clear()
    get_registry.cache_clear()
    get_favorites.cache_clear()


def test_library_route_reports_the_three_planes(library_env):
    record_warm("txROUTE", get_settings(), why="seed")

    def handler(request: httpx.Request) -> httpx.Response:
        kubo = kubo_handler(request)
        if kubo is not None:
            return kubo
        assert request.method == "HEAD"
        return httpx.Response(200, headers={"x-cache": "HIT"})

    real = app_module.app.state.client
    app_module.app.state.client = client_for(handler)
    try:
        response = library_env.get("/library")
    finally:
        app_module.app.state.client = real

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"ipfs", "arweave", "registry"}
    assert body["ipfs"]["pinned"] == 3
    assert body["arweave"]["known_warmed"] == 1 and body["arweave"]["currently_cached"] == 1
    assert body["arweave"]["retained"]["kept"] == 0
    assert body["registry"] == {"overrides": 0, "favorites": 0, "captures": 0}

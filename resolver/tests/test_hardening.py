"""Tests for the operational-hardening pass (code-review findings)."""

import asyncio
import socket

import httpx
import pytest

from resolver.config import Settings
from resolver.resolve import _fetch_allowed, external_url_ok, resolve_ref
from resolver.seed import _JOBS, SeedJob, TooManySeedJobs, _recover_cid, start_seed

SETTINGS = Settings(
    ipfs_internal="http://ipfs.internal",
    arweave_internal="http://ar.internal",
    ipfs_public_base="http://box:8080",
    arweave_public_base="http://box:3000",
)


@pytest.fixture(autouse=True)
def clean_jobs():
    _JOBS.clear()
    yield
    _JOBS.clear()


# --- finding 2: SSRF guards + bounded fetches ---


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://example.com/x", True),
        ("http://8.8.8.8/x", True),
        ("http://127.0.0.1:5001/api/v0/pin/add", False),
        ("http://localhost:8090/seed", False),
        ("http://192.168.7.13:1111/reboot", False),
        ("http://10.0.0.1/", False),
        ("http://169.254.169.254/latest/meta-data", False),
        ("ftp://example.com/x", False),
    ],
)
def test_external_url_guard(url, ok):
    assert external_url_ok(url) is ok


@pytest.mark.parametrize(
    "url,ok",
    [
        ("http://127.0.0.1:3000", True),  # exact arweave base
        ("http://127.0.0.1:3000/txid", True),
        ("http://127.0.0.1:8080/ipfs/bafyCID/art.png", True),
        ("http://127.0.0.1:30001/steal", False),  # look-alike port
        ("http://127.0.0.1:30001", False),
        ("http://127.0.0.1:8080x.evil.com/", False),
    ],
)
def test_gateway_exemption_requires_path_boundary(url, ok):
    local = Settings(
        ipfs_internal="http://127.0.0.1:8080",
        arweave_internal="http://127.0.0.1:3000/",  # trailing slash must not break it
    )
    assert _fetch_allowed(url, local) is ok


async def test_dns_rebinding_target_is_refused_before_connection(monkeypatch):
    def private_dns(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", private_dns)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(AssertionError("must not connect"))
    )) as client:
        result = await resolve_ref("https://attacker.example/media.png", SETTINGS, client)
    assert result.resolved is False
    assert "internal/private" in (result.note or "")


async def test_seed_recovery_revalidates_redirect_dns_before_contacting_it(monkeypatch):
    def dns(host, *_args, **_kwargs):
        address = "8.8.8.8" if host == "safe.example" else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    contacted = []

    def handler(request: httpx.Request) -> httpx.Response:
        contacted.append(str(request.url))
        if request.url.host == "8.8.8.8":
            return httpx.Response(302, headers={"location": "https://rebound.example/private"})
        raise AssertionError("private redirect target must never be contacted")

    job = SeedJob(id="recover", ref="x", chain="ethereum")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert not await _recover_cid("bafyCID", ["https://safe.example/media"], job, SETTINGS, client)
    assert contacted == ["https://8.8.8.8/media"]


async def test_direct_resolution_refuses_private_targets():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network call: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await resolve_ref("http://192.168.7.13:1111/api/status", SETTINGS, client)
    assert result.resolved is False
    assert "internal/private" in (result.note or "")


async def test_oversized_metadata_is_refused():
    small = Settings(
        ipfs_internal="http://ipfs.internal",
        ipfs_public_base="http://box:8080",
        fetch_max_bytes=64,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"name": "big", "padding": "x" * 500},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await resolve_ref("ipfs://bafyMETA/meta.json", small, client)
    assert result.resolved is False
    assert "larger than" in (result.note or "")


# --- finding 6: health semantics ---


async def test_participation_does_not_claim_docker_private_addresses_public():
    from resolver.health import gateway_health
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v0/id":
            return httpx.Response(200, json={"Addresses": ["/ip4/172.18.0.2/tcp/4001", "/ip4/8.8.8.8/tcp/4001"]})
        return httpx.Response(404)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await gateway_health(SETTINGS, client)
    participation = result["participation"]["ipfs"]
    assert participation["status"] == "unknown"
    assert participation["observed_public_addresses"] == ["/ip4/8.8.8.8/tcp/4001"]


async def test_health_reports_5xx_backend_as_down_and_404_as_up():
    from resolver.health import gateway_health

    def handler(request: httpx.Request) -> httpx.Response:
        if "ipfs.internal" in str(request.url):
            return httpx.Response(404)  # Kubo's healthy gateway root
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await gateway_health(SETTINGS, client)
    assert result["backends"]["ipfs"]["ok"] is True
    assert result["backends"]["arweave"]["ok"] is False
    assert result["healthy"] is False


# --- finding 8: unknown extensions get probed, not trusted ---


async def test_unknown_extension_is_probed_not_assumed_media():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await resolve_ref("https://example.com/artwork.php", SETTINGS, client)
    assert result.playback_method == "send"
    assert "/media/" in result.resolved_url


async def test_known_media_extension_skips_the_probe():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"media")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await resolve_ref("https://example.com/artwork.mp4", SETTINGS, client)
    assert result.playback_method == "play"
    assert result.resolved is True
    assert "/media/" in result.resolved_url


# --- finding 3: /c must not redirect unresolved results ---


def test_cast_route_refuses_unresolved_refs(http_client):
    response = http_client.get("/c", params={"ref": "not a reference"}, follow_redirects=False)
    assert response.status_code == 422
    assert response.json()["resolved"] is False

    ok = http_client.get(
        "/c", params={"ref": "ipfs://bafyCID/art.png"}, follow_redirects=False
    )
    assert ok.status_code == 422  # known suffix cannot bypass local availability
    assert ok.json()["resolved"] is False


# --- finding 1: seed admission control ---


# Address-shaped refs: admission control resolves names up front, so a
# name ref against the blocking client would deadlock start_seed itself.
# Addresses pass through without a network call; the blocking client then
# holds the *enumeration* open, keeping the first job in "running".
_TZ_ADDR = "tz1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_ETH_ADDR = "0x" + "a" * 40


def _blocking_client(release: asyncio.Event) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_duplicate_wallet_seed_returns_the_running_job():
    release = asyncio.Event()
    async with _blocking_client(release) as client:
        first = await start_seed(_TZ_ADDR, SETTINGS, client)
        second = await start_seed(_TZ_ADDR, SETTINGS, client)
        assert first is not None and second is not None
        assert second.id == first.id
        release.set()
        await asyncio.sleep(0)


async def test_active_job_cap_rejects_new_wallets():
    capped = Settings(seed_max_active=1)
    release = asyncio.Event()
    async with _blocking_client(release) as client:
        first = await start_seed(_TZ_ADDR, capped, client)
        assert first is not None
        with pytest.raises(TooManySeedJobs):
            await start_seed(_ETH_ADDR, capped, client)
        release.set()
        await asyncio.sleep(0)


async def test_finished_job_history_is_bounded():
    tiny = Settings(seed_jobs_kept=2)
    for i in range(5):
        _JOBS[f"old{i}"] = SeedJob(id=f"old{i}", ref=f"w{i}", chain="tezos", status="done")

    release = asyncio.Event()
    release.set()
    async with _blocking_client(release) as client:
        job = await start_seed(_TZ_ADDR, tiny, client)
        assert job is not None
        await asyncio.sleep(0.01)
    finished = [j for j in _JOBS.values() if j.status != "running" and j.id.startswith("old")]
    assert len(finished) <= 2


async def test_seed_wall_clock_cap_fails_the_job():
    from resolver.seed import run_seed

    slow = Settings(
        tzkt_base="http://tzkt.internal/v1",
        seed_max_seconds=0.05,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(404)

    job = SeedJob(id="t1", ref="tz1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", chain="tezos", started_at="t")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_seed(job, slow, client)
    assert job.status == "failed"
    assert any("wall-clock" in e for e in job.errors)

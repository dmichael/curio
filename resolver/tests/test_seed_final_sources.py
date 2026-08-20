"""Seed retention follows the final media plane, never its metadata wrapper."""
import httpx

from resolver.config import Settings
from resolver.seed import SeedJob, run_seed
from resolver.static_store import StaticStore

ETH_ADDR = "0xAbAbAbAbAbAbAbAbAbAbAbAbAbAbAbAbAbAbAbAb"
MEDIA_TXID = "M" * 43


async def test_seed_keeps_http_and_data_metadata_on_final_native_planes(tmp_path):
    settings = Settings(
        blockscout_base="http://bs.internal/api/v2",
        ipfs_internal="http://ipfs.internal",
        ipfs_api="http://kubo.internal",
        arweave_internal="http://ar.internal",
        static_root=str(tmp_path / "media"),
        ssrf_dns_check=False,
    )
    ipfs_metadata = "https://metadata.example/ipfs.json"
    ar_metadata = "https://metadata.example/ar.json"
    data_metadata = 'data:application/json,{"image":"data:image/svg+xml,%3Csvg/%3E"}'
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url}")
        if request.url.path.endswith("/nft"):
            return httpx.Response(200, json={"items": [{"metadata": {
                "image": ipfs_metadata, "animation_url": ar_metadata, "displayUri": data_metadata,
            }}], "next_page_params": None})
        if str(request.url) == ipfs_metadata:
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"image": "ipfs://bafyMEDIA/art.png"})
        if str(request.url) == ar_metadata:
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"image": f"ar://{MEDIA_TXID}"})
        if request.url.host == "ipfs.internal" and request.method == "HEAD":
            return httpx.Response(200, headers={"content-type": "image/png"})
        if request.url.host == "ar.internal" and request.method == "HEAD":
            return httpx.Response(200, headers={"content-type": "image/png"})
        if request.url.host == "ar.internal" and request.method == "GET":
            return httpx.Response(200, headers={"x-cache": "HIT"}, content=b"ar")
        if request.url.path == "/api/v0/pin/add":
            return httpx.Response(200, json={"Pins": ["bafyMEDIA"]})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    job = SeedJob(id="seedtest", ref=ETH_ADDR, chain="ethereum", address=ETH_ADDR, started_at="t")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_seed(job, settings, client)

    assert job.status == "done", job.errors
    assert (job.pinned, job.warmed, job.captured) == (1, 1, 1)
    assert not any("/api/v0/add" in request for request in seen)
    assert any("/api/v0/pin/add" in request for request in seen)


async def test_seed_stores_native_html_and_records_live_dependency(tmp_path):
    txid = "H" * 43
    settings = Settings(
        blockscout_base="http://bs.internal/api/v2",
        ipfs_internal="http://ipfs.internal",
        ipfs_api="http://kubo.internal",
        arweave_internal="http://ar.internal",
        static_root=str(tmp_path / "media"),
        ssrf_dns_check=False,
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url}")
        if request.url.path.endswith("/nft"):
            return httpx.Response(200, json={"items": [{"metadata": {
                "image": "ipfs://bafyRUNTIME/index.html",
                "animation_url": f"ar://{txid}/index.html",
            }}], "next_page_params": None})
        # An external script keeps the audit's answer live-dependent; a
        # relative-only body would upgrade the work to ready.
        shell = b'<script src="https://cdn.example/lib.js"></script>'
        if request.method == "HEAD" and request.url.host == "ipfs.internal":
            return httpx.Response(200, headers={"content-type": "text/html"})
        if request.method == "GET" and request.url.host == "ipfs.internal":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=shell)
        if request.method == "HEAD" and request.url.host == "ar.internal":
            return httpx.Response(200, headers={"content-type": "text/html"})
        if request.method == "GET" and request.url.host == "ar.internal":
            return httpx.Response(200, headers={"x-cache": "HIT", "content-type": "text/html"}, content=shell)
        if request.url.path == "/api/v0/pin/add":
            return httpx.Response(200, json={"Pins": ["bafyRUNTIME"]})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    job = SeedJob(id="html", ref=ETH_ADDR, chain="ethereum", address=ETH_ADDR, started_at="t")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_seed(job, settings, client)

    assert job.status == "done", job.errors
    assert (job.pinned, job.warmed, job.captured) == (1, 1, 0)
    assert job.failed == 0
    store = StaticStore(str(tmp_path / "media"))
    assert store.resolution("ipfs://bafyRUNTIME/index.html")["status"] == "live-dependent"
    assert store.resolution(f"ar://{txid}/index.html")["status"] == "live-dependent"
    assert any("pin/add" in request for request in seen)

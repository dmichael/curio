import asyncio

import httpx
import pytest

from resolver.config import Settings
from resolver.seed import _JOBS, SeedJob, run_seed, start_seed

SETTINGS = Settings(
    ipfs_internal="http://ipfs.internal",
    arweave_internal="http://ar.internal",
    ipfs_api="http://kubo.internal",
    blockscout_base="http://bs.internal/api/v2",
    bens_base="http://bens.internal/api/v1/1",
    tzkt_base="http://tzkt.internal/v1",
    seed_recovery_gateways=["http://gw.fallback/ipfs"],
)

ETH_ADDR = "0xAbAbAbAbAbAbAbAbAbAbAbAbAbAbAbAbAbAbAbAb"
TZ_ADDR = "tz1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TXID = "abcdefghijklmnopqrstuvwxyz0123456789_ABCDEF"


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


@pytest.fixture(autouse=True)
def clean_jobs():
    _JOBS.clear()
    yield
    _JOBS.clear()


def make_job(ref: str, chain: str) -> SeedJob:
    # run_seed expects the address already resolved (start_seed's job);
    # these direct-run tests all use address-shaped refs.
    return SeedJob(id="test1234", ref=ref, chain=chain, address=ref, started_at="t")


async def wait_done(job: SeedJob) -> None:
    while job.status == "running":
        await asyncio.sleep(0)


async def test_eth_wallet_seed_pins_and_warms():
    nft_page = {
        "items": [
            {
                "id": "1",
                "metadata": {
                    "image": "ipfs://bafyIMG/art.png",
                    "animation_url": f"ar://{TXID}",
                },
            },
            {
                # metadata missing — falls back to the indexer's gateway URL,
                # and the CID is deduped against item 1's.
                "id": "2",
                "metadata": None,
                "image_url": "https://gateway.example/ipfs/bafyIMG",
            },
        ],
        "next_page_params": None,
    }
    routes = {
        f"http://bs.internal/api/v2/addresses/{ETH_ADDR}/nft?type=ERC-721%2CERC-1155": {
            "status_code": 200,
            "json": nft_page,
        },
        "POST http://kubo.internal/api/v0/pin/add?arg=%2Fipfs%2FbafyIMG": {
            "status_code": 200,
            "json": {"Pins": ["bafyIMG"]},
        },
        f"http://127.0.0.1:4001/{TXID}": [{"status_code": 200, "content": b"0" * 100}, {"status_code": 200, "content": b"0" * 100}],
    }
    client, log = fake_net(routes)
    job = make_job(ETH_ADDR, "ethereum")
    async with client:
        await run_seed(job, SETTINGS, client)
    assert job.status == "done", job.errors
    assert job.tokens == 2
    assert job.pinned == 1
    assert job.retained == 1 and job.warmed == 0  # explicit seed uses retained native plane
    assert job.skipped == 0
    assert job.failed == 0
    assert sum("pin/add" in line for line in log) == 1  # deduped across tokens


async def test_ens_name_resolves_via_bens():
    routes = {
        "http://bens.internal/api/v1/1/domains/alice.eth": {
            "status_code": 200,
            "json": {"resolved_address": {"hash": ETH_ADDR}},
        },
        f"http://bs.internal/api/v2/addresses/{ETH_ADDR}/nft?type=ERC-721%2CERC-1155": {
            "status_code": 200,
            "json": {"items": [], "next_page_params": None},
        },
    }
    client, _ = fake_net(routes)
    async with client:
        job = await start_seed("alice.eth", SETTINGS, client)
        assert job is not None
        assert job.address == ETH_ADDR  # resolved before the job runs
        await wait_done(job)
    assert job.status == "done", job.errors


async def test_tezos_domain_seed_uses_tzkt():
    balances_url = (
        "http://tzkt.internal/v1/tokens/balances"
        f"?account={TZ_ADDR}&balance.gt=0&token.standard=fa2&offset=0&limit=200"
        "&select=token.contract.address+as+contract%2Ctoken.tokenId+as+tokenId%2Ctoken.metadata+as+metadata%2Cbalance"
    )
    routes = {
        "http://tzkt.internal/v1/domains?name=alice.tez": {
            "status_code": 200,
            "json": [{"address": {"address": TZ_ADDR}}],
        },
        balances_url: {
            "status_code": 200,
            "json": [
                {
                    "contract": "KT1x",
                    "tokenId": "7",
                    "metadata": {
                        "artifactUri": "ipfs://bafyART",
                        "displayUri": "ipfs://bafyDISP",
                        "formats": [{"uri": "ipfs://bafyFMT", "mimeType": "video/mp4"}],
                    },
                }
            ],
        },
        "POST http://kubo.internal/api/v0/pin/add?arg=%2Fipfs%2FbafyART": {"status_code": 200, "json": {}},
        "POST http://kubo.internal/api/v0/pin/add?arg=%2Fipfs%2FbafyDISP": {"status_code": 200, "json": {}},
        "POST http://kubo.internal/api/v0/pin/add?arg=%2Fipfs%2FbafyFMT": {"status_code": 200, "json": {}},
    }
    client, _ = fake_net(routes)
    async with client:
        job = await start_seed("alice.tez", SETTINGS, client)
        assert job is not None
        assert job.address == TZ_ADDR  # resolved before the job runs
        await wait_done(job)
    assert job.status == "done", job.errors
    assert job.pinned == 3


async def test_created_and_published_jobs_coalesce_separately():
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        published = await start_seed(TZ_ADDR, SETTINGS, client, scope="published")
        created = await start_seed(TZ_ADDR, SETTINGS, client, scope="created")
        assert published is not None and created is not None
        assert created is not published
        assert created.scope == "created"
        again = await start_seed(TZ_ADDR, SETTINGS, client, scope="created")
        assert again is created
        release.set()
        await wait_done(published)
        await wait_done(created)


async def test_held_and_published_jobs_coalesce_separately():
    # Same wallet, different scopes = different jobs; same scope coalesces.
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        await release.wait()  # hold enumeration open so the jobs stay running
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        held = await start_seed(TZ_ADDR, SETTINGS, client)
        published = await start_seed(TZ_ADDR, SETTINGS, client, scope="published")
        assert held is not None and published is not None
        assert published is not held
        assert (held.scope, published.scope) == ("held", "published")
        again = await start_seed(TZ_ADDR, SETTINGS, client, scope="published")
        assert again is published
        release.set()
        await wait_done(held)
        await wait_done(published)


async def test_name_and_address_spellings_coalesce_to_one_job():
    # `alice.eth` and its resolved 0x… address are the same wallet; a second
    # start must return the running job, not double-seed it.
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "http://bens.internal/api/v1/1/domains/alice.eth":
            return httpx.Response(200, json={"resolved_address": {"hash": ETH_ADDR}})
        await release.wait()  # hold enumeration open so the job stays running
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await start_seed("alice.eth", SETTINGS, client)
        second = await start_seed(ETH_ADDR, SETTINGS, client)
        assert first is not None
        assert second is first
        assert first.ref == "alice.eth"  # the caller's spelling is kept
        release.set()
        await wait_done(first)


async def test_pin_failures_are_counted_not_fatal():
    routes = {
        f"http://bs.internal/api/v2/addresses/{ETH_ADDR}/nft?type=ERC-721%2CERC-1155": {
            "status_code": 200,
            "json": {
                "items": [{"id": "1", "metadata": {"image": "ipfs://bafyGONE"}}],
                "next_page_params": None,
            },
        },
        # no pin route: kubo returns 404
    }
    client, _ = fake_net(routes)
    job = make_job(ETH_ADDR, "ethereum")
    async with client:
        await run_seed(job, SETTINGS, client)
    assert job.status == "done"
    assert job.failed == 1
    assert job.errors


async def test_failed_pin_recovers_from_http_copy():
    source = "https://gw.example/ipfs/bafyLOST"
    routes = {
        f"http://bs.internal/api/v2/addresses/{ETH_ADDR}/nft?type=ERC-721%2CERC-1155": {
            "status_code": 200,
            "json": {"items": [{"id": "1", "metadata": {"image": source}}], "next_page_params": None},
        },
        # first pin attempt fails (no providers); pin after recovery succeeds
        "POST http://kubo.internal/api/v0/pin/add?arg=%2Fipfs%2FbafyLOST": [
            {"status_code": 500, "text": "context deadline exceeded"},
            {"status_code": 200, "json": {"Pins": ["bafyLOST"]}},
        ],
        source: {"status_code": 200, "content": b"the artwork bytes"},
        "POST http://kubo.internal/api/v0/add?pin=false&cid-version=1": {
            "status_code": 200,
            "json": {"Name": "recovered", "Hash": "bafyLOST", "Size": "17"},
        },
    }
    client, log = fake_net(routes)
    job = make_job(ETH_ADDR, "ethereum")
    async with client:
        await run_seed(job, SETTINGS, client)
    assert job.status == "done", job.errors
    assert job.recovered == 1
    assert job.pinned == 0
    assert job.failed == 0
    assert any("/api/v0/add" in line for line in log)


async def test_ipfs_scheme_ref_recovers_via_public_gateway_fallback():
    # ipfs:// refs carry no HTTP source of their own; the configured public
    # gateway is tried as the recovery copy.
    routes = {
        f"http://bs.internal/api/v2/addresses/{ETH_ADDR}/nft?type=ERC-721%2CERC-1155": {
            "status_code": 200,
            "json": {"items": [{"id": "1", "metadata": {"image": "ipfs://bafyLOST"}}], "next_page_params": None},
        },
        "POST http://kubo.internal/api/v0/pin/add?arg=%2Fipfs%2FbafyLOST": [
            {"status_code": 500, "text": "context deadline exceeded"},
            {"status_code": 200, "json": {"Pins": ["bafyLOST"]}},
        ],
        "http://gw.fallback/ipfs/bafyLOST": {"status_code": 200, "content": b"the artwork bytes"},
        "POST http://kubo.internal/api/v0/add?pin=false&cid-version=1": {
            "status_code": 200,
            "json": {"Name": "recovered", "Hash": "bafyLOST", "Size": "17"},
        },
    }
    client, _ = fake_net(routes)
    job = make_job(ETH_ADDR, "ethereum")
    async with client:
        await run_seed(job, SETTINGS, client)
    assert job.status == "done", job.errors
    assert job.recovered == 1
    assert job.failed == 0


async def test_recovery_rejects_bytes_that_hash_differently():
    source = "https://gw.example/ipfs/QmLOST"
    routes = {
        f"http://bs.internal/api/v2/addresses/{ETH_ADDR}/nft?type=ERC-721%2CERC-1155": {
            "status_code": 200,
            "json": {"items": [{"id": "1", "metadata": {"image": source}}], "next_page_params": None},
        },
        source: {"status_code": 200, "content": b"tampered bytes"},
        "POST http://kubo.internal/api/v0/add?pin=false": {
            "status_code": 200,
            "json": {"Name": "recovered", "Hash": "QmSOMETHINGELSE", "Size": "14"},
        },
        # pin/add for QmLOST is unrouted -> 404 -> pin fails, recovery tried
    }
    client, _ = fake_net(routes)
    job = make_job(ETH_ADDR, "ethereum")
    async with client:
        await run_seed(job, SETTINGS, client)
    assert job.status == "done"
    assert job.recovered == 0
    assert job.failed == 1


async def test_limit_stops_enumeration():
    routes = {
        f"http://bs.internal/api/v2/addresses/{ETH_ADDR}/nft?type=ERC-721%2CERC-1155": {
            "status_code": 200,
            "json": {
                "items": [
                    {"id": "1", "metadata": {"image": "ipfs://bafyA"}},
                    {"id": "2", "metadata": {"image": "ipfs://bafyB"}},
                ],
                # a next page that would 404 if fetched
                "next_page_params": {"token_id": "999"},
            },
        },
        "POST http://kubo.internal/api/v0/pin/add?arg=%2Fipfs%2FbafyA": {"status_code": 200, "json": {}},
    }
    client, _ = fake_net(routes)
    job = make_job(ETH_ADDR, "ethereum")
    async with client:
        await run_seed(job, SETTINGS, client, limit=1)
    assert job.status == "done", job.errors
    assert job.tokens == 1
    assert job.pinned == 1

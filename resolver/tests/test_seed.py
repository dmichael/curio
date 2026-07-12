import asyncio

import httpx
import pytest

from resolver.config import Settings
from resolver.seed import (
    _JOBS,
    SeedJob,
    _eth_contract_items,
    _tezos_contract_items,
    _tezos_published_items,
    classify_wallet,
    list_wallet_tokens,
    run_seed,
    start_seed,
)

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
KT1_ADDR = "KT1" + "a" * 33
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


@pytest.mark.parametrize(
    "ref,chain",
    [
        (ETH_ADDR, "ethereum"),
        ("alice.eth", "ethereum"),
        ("name💎.eth", "ethereum"),
        (TZ_ADDR, "tezos"),
        ("alice.tez", "tezos"),
    ],
)
def test_wallet_refs_are_classified(ref, chain):
    assert classify_wallet(ref) == chain


@pytest.mark.parametrize("ref", ["ipfs://bafyCID", "https://example.com/x.png", "0x1234", "hello"])
def test_non_wallet_refs_are_rejected(ref):
    assert classify_wallet(ref) is None


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
        f"http://ar.internal/{TXID}": {"status_code": 200, "content": b"0" * 100},
    }
    client, log = fake_net(routes)
    job = make_job(ETH_ADDR, "ethereum")
    async with client:
        await run_seed(job, SETTINGS, client)
    assert job.status == "done", job.errors
    assert job.tokens == 2
    assert job.pinned == 1
    assert job.warmed == 1
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


def published_url(offset: int) -> str:
    return (
        "http://tzkt.internal/v1/tokens"
        f"?firstMinter={TZ_ADDR}&offset={offset}&limit=200"
        "&select=contract%2CtokenId%2Cmetadata"
    )


async def test_published_items_normalize_contract_and_page():
    # TzKT's /tokens selects `contract` to an object; the enumerator must
    # flatten it to the bare address _tezos_items yields, and page by offset.
    first_page = [
        {
            "contract": {"alias": "objkt", "address": f"KT1c{i}"},
            "tokenId": str(i),
            "metadata": {"artifactUri": "ipfs://bafyART"},
        }
        for i in range(200)
    ]
    second_page = [{"contract": {"alias": None, "address": "KT1last"}, "tokenId": "200", "metadata": None}]
    routes = {
        published_url(0): {"status_code": 200, "json": first_page},
        published_url(200): {"status_code": 200, "json": second_page},
    }
    client, _ = fake_net(routes)
    async with client:
        items = [item async for item in _tezos_published_items(TZ_ADDR, SETTINGS, client)]
    assert len(items) == 201
    assert items[0] == {"contract": "KT1c0", "tokenId": "0", "metadata": {"artifactUri": "ipfs://bafyART"}}
    # empty metadata still counts as a token — it just contributes no refs
    assert items[-1] == {"contract": "KT1last", "tokenId": "200", "metadata": {}}


async def test_published_scope_is_tezos_only():
    client, _ = fake_net({})
    async with client:
        with pytest.raises(ValueError, match="tezos-only"):
            await start_seed(ETH_ADDR, SETTINGS, client, scope="published")


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


def contract_url(offset: int) -> str:
    return (
        "http://tzkt.internal/v1/tokens"
        f"?contract={KT1_ADDR}&offset={offset}&limit=200"
        "&select=contract%2CtokenId%2Cmetadata"
    )


async def test_eth_contract_items_normalize_and_page():
    # Blockscout token instances page via next_page_params (no `type` param)
    # and are reshaped to the holdings item shape _token_record/_media_refs
    # expect — including the item-level image_url/animation_url.
    base = f"http://bs.internal/api/v2/tokens/{ETH_ADDR}/instances"
    routes = {
        base: {
            "status_code": 200,
            "json": {
                "items": [
                    {
                        "id": "1",
                        "metadata": {"image": "ipfs://bafyIMG"},
                        "media_type": "image/png",
                        "image_url": "https://gw.example/ipfs/bafyIMG",
                        "animation_url": None,
                        "owner": {"hash": "0x" + "d" * 40},
                    }
                ],
                "next_page_params": {"unique_token": 42},
            },
        },
        f"{base}?unique_token=42": {
            "status_code": 200,
            "json": {"items": [{"id": "2", "metadata": None}], "next_page_params": None},
        },
    }
    client, _ = fake_net(routes)
    async with client:
        items = [item async for item in _eth_contract_items(ETH_ADDR, SETTINGS, client)]
    assert len(items) == 2
    assert items[0]["token"] == {"address_hash": ETH_ADDR}
    assert items[0]["id"] == "1"
    assert items[0]["media_type"] == "image/png"
    assert items[0]["image_url"] == "https://gw.example/ipfs/bafyIMG"
    assert items[1] == {
        "token": {"address_hash": ETH_ADDR},
        "id": "2",
        "metadata": {},
        "media_type": None,
        "image_url": None,
        "animation_url": None,
    }


async def test_tezos_contract_items_normalize():
    routes = {
        contract_url(0): {
            "status_code": 200,
            "json": [
                {
                    "contract": {"alias": "gallery", "address": KT1_ADDR},
                    "tokenId": "3",
                    "metadata": {"artifactUri": "ipfs://bafyART"},
                }
            ],
        },
    }
    client, _ = fake_net(routes)
    async with client:
        items = [item async for item in _tezos_contract_items(KT1_ADDR, SETTINGS, client)]
    assert items == [{"contract": KT1_ADDR, "tokenId": "3", "metadata": {"artifactUri": "ipfs://bafyART"}}]


async def test_contract_scope_rejects_names():
    # A name resolves to an account, never a contract; contract scope wants
    # the literal address.
    client, _ = fake_net({})
    async with client:
        with pytest.raises(ValueError, match="scope"):
            await start_seed("alice.eth", SETTINGS, client, scope="contract")


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


async def test_wallet_inventory_normalizes_tezos_tokens():
    balances_url = (
        "http://tzkt.internal/v1/tokens/balances"
        f"?account={TZ_ADDR}&balance.gt=0&token.standard=fa2&offset=0&limit=200"
        "&select=token.contract.address+as+contract%2Ctoken.tokenId+as+tokenId%2Ctoken.metadata+as+metadata%2Cbalance"
    )
    routes = {
        balances_url: {
            "status_code": 200,
            "json": [
                {
                    "contract": "KT1abc",
                    "tokenId": "7",
                    "metadata": {
                        "name": "Temple VII",
                        "artifactUri": "ipfs://bafyART",
                        "displayUri": "ipfs://bafyDISP",
                        "formats": [{"uri": "ipfs://bafyART", "mimeType": "image/jpeg"}],
                    },
                }
            ],
        },
    }
    client, _ = fake_net(routes)
    async with client:
        result = await list_wallet_tokens(TZ_ADDR, SETTINGS, client)
    assert result["chain"] == "tezos"
    assert result["count"] == 1
    token = result["tokens"][0]
    assert token["name"] == "Temple VII"
    assert token["contract"] == "KT1abc"
    assert token["token_id"] == "7"
    assert token["mime"] == "image/jpeg"
    assert token["primary_ref"] == "ipfs://bafyART"
    assert "ipfs://bafyDISP" in token["refs"]


async def test_wallet_inventory_normalizes_eth_tokens():
    routes = {
        f"http://bs.internal/api/v2/addresses/{ETH_ADDR}/nft?type=ERC-721%2CERC-1155": {
            "status_code": 200,
            "json": {
                "items": [
                    {
                        "id": "309",
                        "media_type": "image/png",
                        "metadata": {"name": "Études #310", "image": "ipfs://bafyIMG"},
                        "token": {"address_hash": "0xC0FFEE", "name": "Études"},
                    }
                ],
                "next_page_params": None,
            },
        },
    }
    client, _ = fake_net(routes)
    async with client:
        result = await list_wallet_tokens(ETH_ADDR, SETTINGS, client)
    token = result["tokens"][0]
    assert token["name"] == "Études #310"
    assert token["contract"] == "0xC0FFEE"
    assert token["token_id"] == "309"
    assert token["primary_ref"] == "ipfs://bafyIMG"


async def test_wallet_inventory_rejects_non_wallets():
    client, _ = fake_net({})
    async with client:
        assert await list_wallet_tokens("ipfs://bafyCID", SETTINGS, client) is None


async def test_wallet_inventory_published_scope():
    routes = {
        published_url(0): {
            "status_code": 200,
            "json": [
                {
                    "contract": {"alias": "objkt", "address": "KT1abc"},
                    "tokenId": "7",
                    "metadata": {"name": "Temple VII", "artifactUri": "ipfs://bafyART"},
                }
            ],
        },
    }
    client, _ = fake_net(routes)
    async with client:
        result = await list_wallet_tokens(TZ_ADDR, SETTINGS, client, scope="published")
    assert result["scope"] == "published"
    assert result["count"] == 1
    token = result["tokens"][0]
    assert token["contract"] == "KT1abc"  # the object flattened to its address
    assert token["name"] == "Temple VII"
    assert token["primary_ref"] == "ipfs://bafyART"


def test_wallet_route_rejects_bogus_scope(http_client):
    response = http_client.get("/wallet", params={"ref": TZ_ADDR, "scope": "bogus"})
    assert response.status_code == 400
    assert "scope" in response.json()["error"]


def test_wallet_route_contract_scope_round_trip(http_client, monkeypatch):
    from resolver import app as app_module
    from resolver.config import get_settings

    monkeypatch.setenv("RESOLVER_TZKT_BASE", "http://tzkt.internal/v1")
    get_settings.cache_clear()
    routes = {
        contract_url(0): {
            "status_code": 200,
            "json": [
                {
                    "contract": {"alias": "gallery", "address": KT1_ADDR},
                    "tokenId": "1",
                    "metadata": {"name": "Edition 1", "artifactUri": "ipfs://bafyART"},
                }
            ],
        },
    }
    fake_client, _ = fake_net(routes)
    real_client = app_module.app.state.client
    app_module.app.state.client = fake_client
    try:
        response = http_client.get("/wallet", params={"ref": KT1_ADDR, "scope": "contract"})
    finally:
        app_module.app.state.client = real_client
        get_settings.cache_clear()
    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == "contract"
    assert body["chain"] == "tezos"
    assert body["count"] == 1
    token = body["tokens"][0]
    assert token["contract"] == KT1_ADDR
    assert token["name"] == "Edition 1"
    assert token["primary_ref"] == "ipfs://bafyART"


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


CAPTURE_URL = "https://hodlers.example/art/149.mp4"
CAPTURE_BYTES = b"the only copy of these bytes"


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
    import hashlib
    import json

    settings = SETTINGS.model_copy(update={"seed_capture_dir": str(tmp_path)})
    client, _ = fake_net(capture_routes())
    job = make_job(ETH_ADDR, "ethereum")
    async with client:
        await run_seed(job, settings, client)
    assert job.status == "done", job.errors
    assert job.captured == 1
    assert job.skipped == 0
    assert job.failed == 0

    lines = (tmp_path / "captures.jsonl").read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["source"] == CAPTURE_URL
    assert record["cid"] == "bafyCAPTURED"
    assert record["sha256"] == hashlib.sha256(CAPTURE_BYTES).hexdigest()
    assert record["bytes"] == len(CAPTURE_BYTES)
    assert record["content_type"] == "video/mp4"
    assert record["wallet"] == ETH_ADDR


async def test_capture_is_once_per_url_across_jobs(tmp_path):
    settings = SETTINGS.model_copy(update={"seed_capture_dir": str(tmp_path)})
    first, _ = fake_net(capture_routes())
    async with first:
        await run_seed(make_job(ETH_ADDR, "ethereum"), settings, first)

    second, log = fake_net(capture_routes())
    job = make_job(ETH_ADDR, "ethereum")
    async with second:
        await run_seed(job, settings, second)
    assert job.status == "done", job.errors
    assert job.captured == 1  # idempotent, like re-pinning
    assert not any(CAPTURE_URL in line for line in log)  # no re-download
    assert len((tmp_path / "captures.jsonl").read_text().splitlines()) == 1


async def test_http_media_is_skipped_when_capture_is_off():
    client, _ = fake_net(capture_routes())
    job = make_job(ETH_ADDR, "ethereum")
    async with client:
        await run_seed(job, SETTINGS, client)
    assert job.status == "done", job.errors
    assert job.captured == 0
    assert job.skipped == 1


async def test_capture_failure_is_counted_not_fatal(tmp_path):
    settings = SETTINGS.model_copy(update={"seed_capture_dir": str(tmp_path)})
    routes = capture_routes()
    routes[CAPTURE_URL] = {"status_code": 410}  # the domain died mid-seed
    client, _ = fake_net(routes)
    job = make_job(ETH_ADDR, "ethereum")
    async with client:
        await run_seed(job, settings, client)
    assert job.status == "done"
    assert job.captured == 0
    assert job.failed == 1
    assert not (tmp_path / "captures.jsonl").exists()


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


# --- status=1: the audit view -----------------------------------------------


async def test_token_status_classification(monkeypatch):
    from resolver import seed as seed_module
    from resolver.resolve import Resolved

    outcomes = {
        "ipfs://bafyOK/art.png": Resolved(
            "r", "http://box:8080/ipfs/bafyOK/art.png", "play", "ipfs", True
        ),
        "ipfs://bafyDEAD": Resolved(
            "r", "http://box:8080/ipfs/bafyDEAD", "play", "ipfs", True,
            note="gateway probe failed; no filename hint",
        ),
        "ipfs://bafyGONE/x": Resolved(
            "r", "http://box:8080/ipfs/bafySUB/m.png", "play", "ipfs", True,
            substituted=True, substituted_ref="ipfs://bafyGONE/x",
            substitution_status="alternate-master",
        ),
        "not a ref": Resolved("r", "not a ref", "play", None, False, note="unrecognized reference"),
    }

    async def fake_resolve(ref, settings, client, _depth=0):
        return outcomes[ref]

    monkeypatch.setattr(seed_module, "resolve_ref", fake_resolve)
    tokens = [{"primary_ref": ref} for ref in outcomes] + [{"primary_ref": None}]
    sem = asyncio.Semaphore(4)
    await asyncio.gather(
        *(seed_module._token_status(t, SETTINGS, None, sem) for t in tokens)
    )
    assert [t["status"] for t in tokens] == [
        "ok", "unreachable", "substituted", "unresolvable", "no-ref"
    ]
    # resolved tokens carry the playable URL; the substituted one points at
    # the replacement, which is exactly what a repair audit wants to see
    assert tokens[0]["resolved_url"] == "http://box:8080/ipfs/bafyOK/art.png"
    assert tokens[2]["resolved_url"] == "http://box:8080/ipfs/bafySUB/m.png"


async def test_wallet_status_audit_and_creators():
    routes = {
        published_url(0): {
            "status_code": 200,
            "json": [
                {
                    "contract": {"alias": "objkt", "address": "KT1abc"},
                    "tokenId": "7",
                    "metadata": {
                        "name": "Temple VII",
                        # known extension resolves mechanically — no probe
                        "artifactUri": "ipfs://bafyART/work.png",
                        "creators": [TZ_ADDR],
                    },
                }
            ],
        },
    }
    client, _ = fake_net(routes)
    async with client:
        result = await list_wallet_tokens(
            TZ_ADDR, SETTINGS, client, scope="published", status=True
        )
    token = result["tokens"][0]
    assert token["status"] == "ok"
    assert token["creators"] == [TZ_ADDR]
    assert token["resolved_url"].endswith("/ipfs/bafyART/work.png")
    assert result["status_counts"] == {"ok": 1}

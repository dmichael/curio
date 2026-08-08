import asyncio

import httpx
import pytest

from resolver.config import Settings
from resolver.seed import start_seed
from resolver.wallets import (
    _eth_contract_items,
    _tezos_contract_items,
    _tezos_created_items,
    _tezos_published_items,
    classify_wallet,
    list_wallet_tokens,
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


def _created_net() -> tuple[httpx.AsyncClient, list[str]]:
    """A wallet that authored two tokens on one contract: token 1 has 1 of 3
    editions burned (keep), token 2 has its only edition burned (fully burned).
    creators carries both; authors carries none."""
    creators = [
        {"contract": {"address": "KT1c"}, "tokenId": "1",
         "metadata": {"artifactUri": "ipfs://one"}, "totalSupply": "3"},
        {"contract": {"address": "KT1c"}, "tokenId": "2",
         "metadata": {"artifactUri": "ipfs://two"}, "totalSupply": "1"},
    ]
    log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        log.append(url)
        if "/tokens/balances" in url:
            # burn-address holdings: token 1 partial (1/3), token 2 full (1/1)
            return httpx.Response(200, json=[
                {"tokenId": "1", "balance": "1"},
                {"tokenId": "2", "balance": "1"},
            ])
        first_page = "offset=0" in url
        if "creators" in url:
            return httpx.Response(200, json=creators if first_page else [])
        if "authors" in url:
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), log


async def test_created_unions_creators_and_authors_and_drops_fully_burned():
    client, log = _created_net()
    async with client:
        items = [item async for item in _tezos_created_items(TZ_ADDR, SETTINGS, client)]
    # Both creators and authors indexes are queried…
    assert any("creators" in u for u in log) and any("authors" in u for u in log)
    # …and the fully-burned token 2 is dropped, the partly-burned token 1 kept.
    assert [item["tokenId"] for item in items] == ["1"]
    assert items[0]["metadata"]["artifactUri"] == "ipfs://one"


async def test_created_include_burned_keeps_everything_and_skips_burn_query():
    client, log = _created_net()
    async with client:
        items = [
            item async for item in _tezos_created_items(
                TZ_ADDR, SETTINGS, client, include_burned=True
            )
        ]
    assert sorted(item["tokenId"] for item in items) == ["1", "2"]
    # include_burned must not spend a burn-detection round-trip.
    assert not any("/tokens/balances" in u for u in log)


async def test_created_scope_is_tezos_only():
    client, _ = fake_net({})
    async with client:
        with pytest.raises(ValueError, match="tezos-only"):
            await start_seed(ETH_ADDR, SETTINGS, client, scope="created")


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


# --- status=1: the audit view -----------------------------------------------


async def test_token_status_classification(monkeypatch):
    from resolver import wallets as wallets_module
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

    monkeypatch.setattr(wallets_module, "resolve_ref", fake_resolve)
    tokens = [{"primary_ref": ref} for ref in outcomes] + [{"primary_ref": None}]
    sem = asyncio.Semaphore(4)
    await asyncio.gather(
        *(wallets_module._token_status(t, SETTINGS, None, sem) for t in tokens)
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
        "http://ipfs.internal/ipfs/bafyART/work.png": {"status_code": 200, "headers": {"content-type": "image/png"}},
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

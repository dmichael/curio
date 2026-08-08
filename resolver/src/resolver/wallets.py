"""What a wallet or contract points at: enumerate NFTs and their media refs.

Serves `GET /wallet` — a live, normalized inventory of a wallet's NFTs
(Blockscout for Ethereum, TzKT for Tezos — keyless public APIs; no snapshot
files, no local database) — and supplies seeding (seed.py) with the same
enumerators. The default scope answers for the wallet's holdings; the other
scopes change the question:

`scope=published` flips the question to "everything this wallet *first-minted*"
(TzKT's firstMinter index; Tezos only — Ethereum has no keyless creator index).
Published works the wallet no longer holds are the rot-prone corner a
holdings seed never touches.

`scope=created` is the robust authorship index: everything crediting the
address in TZIP-21 `creators`/`authors` metadata — what it actually *made*.
First-minter is a leaky proxy for this: it over-captures (fxhash editions the
address minted-but-didn't-author list it as first minter) and under-captures
(an edition minted by a collector is first-minted by them, not the author).
Fully-burned creations — every edition sent to a burn address — are dropped
by default: they were destroyed on purpose, the lowest preservation priority,
not the highest. `include_burned=1` keeps them. Tezos only; Ethereum has no
keyless creator index at all (mint events say who minted, not who authored),
which is the genuinely unsolved corner.

`scope=contract` enumerates every token of one token contract (both chains;
the ref must be the literal contract address). This is how an ETH publication
sweep is done: name the contract the works were minted on, since no keyless
creator index exists there.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from functools import partial
from typing import Any

import httpx

from .config import Settings
from .resolve import pick_media_field, resolve_ref

_ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TEZOS_ADDRESS_RE = re.compile(r"^(tz[123]|KT1)[1-9A-HJ-NP-Za-km-z]{33}$")

# Editions transferred here are burned — destroyed on purpose. A created work
# whose entire supply sits at a burn address is retracted, not preserved. On
# Tezos a burn is an ordinary transfer, so totalSupply is unchanged and the
# burned editions still show up in balances; "fully burned" = burn-held >=
# supply. Batching keeps a work minted on a shared contract (hic et nunc,
# versum) from dragging that contract's entire burn ledger back.
_TEZOS_BURN_ADDRESSES = ("tz1burnburnburnburnburnburnburjAYjjX",)
_TEZOS_PAGE = 1000
_TEZOS_ID_BATCH = 50

# Every field that may carry media; seeding wants all of them, not a winner.
_MEDIA_FIELDS = (
    "image", "image_url", "imageUrl", "image_original_url",
    "animation_url", "animationUrl", "generator_url", "generatorUrl",
    "artifactUri", "artifact_uri", "displayUri", "display_uri",
    "thumbnailUri", "thumbnail_uri",
)


def classify_wallet(ref: str) -> str | None:
    """Return the chain for a wallet-shaped reference, else None."""
    ref = ref.strip()
    if _ETH_ADDRESS_RE.match(ref) or ref.lower().endswith(".eth"):
        return "ethereum"
    if _TEZOS_ADDRESS_RE.match(ref) or ref.lower().endswith(".tez"):
        return "tezos"
    return None


def _check_scope(ref: str, chain: str, scope: str) -> None:
    """Reject scopes we can't enumerate. Every message contains "scope" —
    the routes key on that to answer 400 (caller mistake) instead of 502."""
    if scope not in ("held", "published", "created", "contract"):
        raise ValueError(
            f"unknown scope: {scope!r} (want 'held', 'published', 'created', or 'contract')"
        )
    if scope in ("published", "created") and chain == "ethereum":
        raise ValueError(f"{scope} scope is tezos-only (no keyless ETH creator index)")
    if scope == "contract" and not (_ETH_ADDRESS_RE.match(ref) or _TEZOS_ADDRESS_RE.match(ref)):
        # A name resolves to an account, never to a contract.
        raise ValueError(f"contract scope needs a literal contract address, not a name: {ref}")


def _enumerator(chain: str, scope: str, include_burned: bool = False):
    if scope == "published":
        return _tezos_published_items  # _check_scope already ruled out eth
    if scope == "created":  # _check_scope already ruled out eth
        return partial(_tezos_created_items, include_burned=include_burned)
    if scope == "contract":
        return _eth_contract_items if chain == "ethereum" else _tezos_contract_items
    return _eth_items if chain == "ethereum" else _tezos_items


def _same_address(chain: str, a: str, b: str) -> bool:
    # EIP-55 checksummed vs lowercase spellings are the same account;
    # Tezos base58 is case-sensitive, so compare it exactly.
    return a.lower() == b.lower() if chain == "ethereum" else a == b


async def _resolve_wallet(
    ref: str, chain: str, settings: Settings, client: httpx.AsyncClient
) -> str:
    """Resolve a name to an address; pass addresses through."""
    if chain == "ethereum":
        if _ETH_ADDRESS_RE.match(ref):
            return ref
        response = await client.get(f"{settings.bens_base}/domains/{ref}")
        response.raise_for_status()
        address = (response.json().get("resolved_address") or {}).get("hash")
        if not address:
            raise ValueError(f"ENS name did not resolve: {ref}")
        return address
    if _TEZOS_ADDRESS_RE.match(ref):
        return ref
    response = await client.get(f"{settings.tzkt_base}/domains", params={"name": ref})
    response.raise_for_status()
    domains = response.json()
    if not domains:
        raise ValueError(f"Tezos domain did not resolve: {ref}")
    return domains[0]["address"]["address"]


async def _eth_items(
    address: str, settings: Settings, client: httpx.AsyncClient
) -> AsyncIterator[dict[str, Any]]:
    """Blockscout v2 NFT holdings, all pages. Raw metadata comes inline."""
    url = f"{settings.blockscout_base}/addresses/{address}/nft"
    params: dict[str, Any] = {"type": "ERC-721,ERC-1155"}
    while True:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        for item in data.get("items", []):
            yield item
        next_page = data.get("next_page_params")
        if not next_page:
            return
        params = {**next_page, "type": "ERC-721,ERC-1155"}


async def _tezos_items(
    address: str, settings: Settings, client: httpx.AsyncClient
) -> AsyncIterator[dict[str, Any]]:
    """TzKT FA2 balances, all pages. Metadata comes inline."""
    page_size = 200
    offset = 0
    while True:
        response = await client.get(
            f"{settings.tzkt_base}/tokens/balances",
            params={
                "account": address,
                "balance.gt": "0",
                "token.standard": "fa2",
                "offset": str(offset),
                "limit": str(page_size),
                "select": "token.contract.address as contract,token.tokenId as tokenId,token.metadata as metadata,balance",
            },
        )
        response.raise_for_status()
        page = response.json()
        for item in page:
            yield item
        if len(page) < page_size:
            return
        offset += page_size


async def _tezos_tokens(
    filters: dict[str, str],
    settings: Settings,
    client: httpx.AsyncClient,
    extra_select: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """TzKT /tokens under a filter, all pages, normalized to the shape
    _tezos_items yields (`contract` selects to an object here, not a bare
    address). `extra_select` requests one more column (e.g. totalSupply) and
    carries it onto the yielded item when present — kept off the default
    select so published/contract queries are byte-for-byte unchanged."""
    page_size = 200
    offset = 0
    select = "contract,tokenId,metadata"
    if extra_select:
        select = f"{select},{extra_select}"
    while True:
        response = await client.get(
            f"{settings.tzkt_base}/tokens",
            params={
                **filters,
                "offset": str(offset),
                "limit": str(page_size),
                "select": select,
            },
        )
        response.raise_for_status()
        page = response.json()
        for item in page:
            contract = item.get("contract") or {}
            record = {
                "contract": contract.get("address"),
                "tokenId": item.get("tokenId"),
                "metadata": item.get("metadata") or {},
            }
            if extra_select and extra_select in item:
                record[extra_select] = item[extra_select]
            yield record
        if len(page) < page_size:
            return
        offset += page_size


def _tezos_published_items(
    address: str, settings: Settings, client: httpx.AsyncClient
) -> AsyncIterator[dict[str, Any]]:
    """TzKT tokens the address first-minted — its published catalog, whoever
    holds the works now."""
    return _tezos_tokens({"firstMinter": address}, settings, client)


# TZIP-21 authorship lives in `creators` (most platforms) or `authors`
# (fxhash); a work counts as created if the address is in either. Queried as
# JSON array-containment filters, which TzKT indexes directly.
_TEZOS_CREATOR_FIELDS = ("metadata.creators.[*]", "metadata.authors.[*]")


async def _tezos_created_items(
    address: str,
    settings: Settings,
    client: httpx.AsyncClient,
    include_burned: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """Tokens crediting `address` in creators/authors metadata — what it
    actually authored, not merely first-minted. Fully-burned creations are
    dropped unless include_burned: destroyed on purpose, they are the lowest
    preservation priority. totalSupply rides along for the burn check."""
    seen: dict[tuple[str | None, Any], dict[str, Any]] = {}
    for field_name in _TEZOS_CREATOR_FIELDS:
        async for item in _tezos_tokens(
            {field_name: address}, settings, client, extra_select="totalSupply"
        ):
            seen.setdefault((item["contract"], item["tokenId"]), item)
    tokens = list(seen.values())
    if not include_burned:
        burned = await _tezos_fully_burned(tokens, settings, client)
        tokens = [t for t in tokens if (t["contract"], t["tokenId"]) not in burned]
    for item in tokens:
        yield item


async def _tezos_fully_burned(
    tokens: list[dict[str, Any]], settings: Settings, client: httpx.AsyncClient
) -> set[tuple[str | None, Any]]:
    """The (contract, tokenId) keys whose entire supply sits at a burn address.

    Burn holdings are queried per contract, filtered to just these token ids,
    so a work minted on a shared contract doesn't pull that contract's whole
    burn ledger. A token whose supply is zero (burned down to nothing, or
    never live) counts as burned; unknown supply is left in — we don't guess.
    """
    supply: dict[tuple[str | None, Any], int | None] = {}
    ids_by_contract: dict[str, list[str]] = {}
    for token in tokens:
        key = (token["contract"], token["tokenId"])
        raw = token.get("totalSupply")
        supply[key] = int(raw) if raw is not None else None
        if token["contract"] is not None:
            ids_by_contract.setdefault(token["contract"], []).append(str(token["tokenId"]))

    burn_held: dict[tuple[str | None, Any], int] = {}
    account = ",".join(_TEZOS_BURN_ADDRESSES)
    for contract, token_ids in ids_by_contract.items():
        for start in range(0, len(token_ids), _TEZOS_ID_BATCH):
            batch = token_ids[start : start + _TEZOS_ID_BATCH]
            offset = 0
            while True:
                response = await client.get(
                    f"{settings.tzkt_base}/tokens/balances",
                    params={
                        "account.in": account,
                        "token.contract": contract,
                        "token.tokenId.in": ",".join(batch),
                        "balance.gt": "0",
                        "offset": str(offset),
                        "limit": str(_TEZOS_PAGE),
                        "select": "token.tokenId as tokenId,balance",
                    },
                )
                response.raise_for_status()
                page = response.json()
                for row in page:
                    key = (contract, str(row["tokenId"]))
                    burn_held[key] = burn_held.get(key, 0) + int(row["balance"])
                if len(page) < _TEZOS_PAGE:
                    break
                offset += _TEZOS_PAGE

    burned: set[tuple[str | None, Any]] = set()
    for key, total in supply.items():
        if total is None:
            continue
        held = burn_held.get((key[0], str(key[1])), 0)
        if total <= 0 or held >= total:
            burned.add(key)
    return burned


def _tezos_contract_items(
    contract: str, settings: Settings, client: httpx.AsyncClient
) -> AsyncIterator[dict[str, Any]]:
    """Every token a Tezos contract ever issued."""
    return _tezos_tokens({"contract": contract}, settings, client)


async def _eth_contract_items(
    contract: str, settings: Settings, client: httpx.AsyncClient
) -> AsyncIterator[dict[str, Any]]:
    """Blockscout v2 token instances — every token the contract ever issued,
    all pages. Normalized to the holdings item shape so _media_refs and
    _token_record just work; the item-level image_url/animation_url are kept
    because _media_refs scans the item dict too."""
    url = f"{settings.blockscout_base}/tokens/{contract}/instances"
    params: dict[str, Any] = {}
    while True:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        for item in data.get("items", []):
            yield {
                "token": {"address_hash": contract},
                "id": item.get("id"),
                "metadata": item.get("metadata") or {},
                "media_type": item.get("media_type"),
                "image_url": item.get("image_url"),
                "animation_url": item.get("animation_url"),
            }
        next_page = data.get("next_page_params")
        if not next_page:
            return
        params = next_page


async def list_wallet_tokens(
    ref: str,
    settings: Settings,
    client: httpx.AsyncClient,
    limit: int | None = None,
    scope: str = "held",
    status: bool = False,
    include_burned: bool = False,
) -> dict[str, Any] | None:
    """Live, normalized inventory of a wallet's NFTs — the browse/pick step.

    Enumerates the same indexers seeding uses (no snapshot files, no local
    database); each token carries its media refs so a consumer can hand the
    chosen one straight to /resolve. None when `ref` isn't wallet-shaped.
    scope="published" lists the works the wallet first-minted (Tezos only)
    instead of its holdings; scope="created" lists what the wallet authored
    (creators/authors metadata, Tezos only), dropping fully-burned works
    unless include_burned; scope="contract" lists every token a token
    contract ever issued (ref must be the literal contract address).

    status=True additionally resolves each token's primary_ref and classifies
    it (see _token_status), so an audit is one call instead of a client-side
    loop. Alive local content answers in milliseconds; each genuinely dead
    ref costs up to the probe timeout, bounded by _STATUS_CONCURRENCY.
    """
    ref = ref.strip()
    chain = classify_wallet(ref)
    if chain is None:
        return None
    _check_scope(ref, chain, scope)
    address = ref if scope == "contract" else await _resolve_wallet(ref, chain, settings, client)
    items = _enumerator(chain, scope, include_burned)
    tokens: list[dict[str, Any]] = []
    async for item in items(address, settings, client):
        tokens.append(_token_record(chain, item))
        if limit is not None and len(tokens) >= limit:
            break
    result: dict[str, Any] = {
        "ref": ref,
        "chain": chain,
        "address": address,
        "scope": scope,
        "count": len(tokens),
        "tokens": tokens,
    }
    if status:
        sem = asyncio.Semaphore(_STATUS_CONCURRENCY)
        await asyncio.gather(*(_token_status(t, settings, client, sem) for t in tokens))
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token["status"]] = counts.get(token["status"], 0) + 1
        result["status_counts"] = counts
    return result


# Probes for alive content hit the local gateway (milliseconds); only dead
# refs are slow (probe timeout), so a wide semaphore keeps a big audit's
# wall-clock near max(dead refs) rather than their sum.
_STATUS_CONCURRENCY = 16


async def _token_status(
    token: dict[str, Any],
    settings: Settings,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
) -> None:
    """Classify a token's primary_ref in place:

      ok           resolves, no failure detected (for probed refs this means
                   the bytes are fetchable; direct URLs with trusted
                   extensions are not probed — resolution, not proof)
      substituted  served via the override registry (already repaired)
      unreachable  recognized but the gateway can't fetch it (dead CID) or
                   resolution errored
      unresolvable resolver recognized the ref and gave up (resolved: false)
      no-ref       token carries no primary media reference
    """
    ref = token.get("primary_ref")
    if not ref:
        token["status"] = "no-ref"
        return
    async with sem:
        try:
            result = await resolve_ref(ref, settings, client)
        except Exception:
            token["status"] = "unreachable"
            return
    if result.substituted:
        token["status"] = "substituted"
    elif not result.resolved:
        token["status"] = "unresolvable"
    elif result.note and "probe failed" in result.note:
        # resolve.py degrades gracefully: a bare CID whose gateway probe
        # failed still "resolves" mechanically, with this note. That is the
        # dead-CID signature; the substring is pinned by a test.
        token["status"] = "unreachable"
    else:
        token["status"] = "ok"
    if result.resolved:
        token["resolved_url"] = result.resolved_url


def _token_record(chain: str, item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata") or {}
    if chain == "ethereum":
        token = item.get("token") or {}
        contract = token.get("address_hash")
        token_id = item.get("id")
        mime = item.get("media_type")
        name = metadata.get("name") or token.get("name")
    else:
        contract = item.get("contract")
        token_id = item.get("tokenId")
        formats = metadata.get("formats") or []
        mime = formats[0].get("mimeType") if formats and isinstance(formats[0], dict) else None
        name = metadata.get("name")
    # TZIP-21 creators — lets a published-catalog consumer split authored
    # works from first-minted collects (fxhash editions list the collector
    # as first minter) without re-fetching metadata.
    creators = metadata.get("creators")
    refs = _media_refs(item)
    return {
        "chain": chain,
        "contract": contract,
        "token_id": token_id,
        "name": name if isinstance(name, str) else None,
        "mime": mime if isinstance(mime, str) else None,
        "creators": creators if isinstance(creators, list) else None,
        "primary_ref": pick_media_field(metadata) or (refs[0] if refs else None),
        "refs": refs,
    }


def _media_refs(item: dict[str, Any]) -> list[str]:
    """Every media reference in a holdings item: metadata fields, tezos
    formats[], and the indexer's own resolved URLs (they still carry the CID
    when metadata is missing)."""
    refs: list[str] = []
    metadata = item.get("metadata") or {}
    sources: list[dict[str, Any]] = [metadata, item]
    for source in sources:
        for key in _MEDIA_FIELDS:
            value = source.get(key)
            if isinstance(value, str) and value and value not in refs:
                refs.append(value)
    formats = metadata.get("formats")
    if isinstance(formats, list):
        for entry in formats:
            uri = entry.get("uri") if isinstance(entry, dict) else None
            if isinstance(uri, str) and uri and uri not in refs:
                refs.append(uri)
    return refs

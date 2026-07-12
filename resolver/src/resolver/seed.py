"""Seed the box's caches from a wallet: pin IPFS media, warm the Arweave cache.

`POST /seed?ref=<0x…|name.eth|tz1…|name.tez>` enumerates the wallet's NFTs
(Blockscout for Ethereum, TzKT for Tezos — keyless public APIs), extracts every
content-addressed media reference from token metadata, then:

  - IPFS refs    -> `pin add` on the box's Kubo API (fetches and keeps the DAG)
  - Arweave refs -> a full GET through the box's ar-io gateway (caches on read)
  - plain http   -> captured into Kubo with provenance recorded (when
    `seed_capture_dir` is set), else counted as skipped

Capture exists because unhashed HTTP media is exactly what vanishes without
recourse: there is no content address to recover against once the domain
dies. Capturing while the URL still answers records source, time, size,
sha256, and the new CID — the strongest provenance an unhashed work can get.
Serving a captured copy later is an operator decision made in the override
registry (overrides.py), never automatic.

Seeding is a background job: POST returns 202 with a job id immediately;
poll GET /seed/{id}. Jobs live in memory only — a restart forgets history
(the pins themselves, of course, persist in Kubo).

This is the *hot* cache tier; a site may separately maintain a curated deep
archive elsewhere. This endpoint answers "make everything this wallet holds
locally servable, now."

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
import hashlib
import json
import logging
import re
import tempfile
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Settings
from .resolve import arweave_txid, external_url_ok, ipfs_parts, pick_media_field, resolve_ref

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
_MAX_ERRORS = 20  # job JSON keeps a sample; the service log gets everything

_log = logging.getLogger("resolver.seed")


@dataclass
class SeedJob:
    id: str
    ref: str
    chain: str  # "ethereum" | "tezos"
    scope: str = "held"  # "held" | "published" (first-minted) | "created" (authored) | "contract"
    include_burned: bool = False  # created scope: keep fully-burned creations too
    status: str = "running"  # "running" | "done" | "failed"
    address: str | None = None
    tokens: int = 0
    refs_found: int = 0
    pinned: int = 0
    recovered: int = 0  # pinned via HTTP-copy recovery after IPFS fetch failed
    warmed: int = 0
    captured: int = 0  # unhashed HTTP media archived into Kubo with provenance
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


_JOBS: dict[str, SeedJob] = {}
_TASKS: set[asyncio.Task[None]] = set()


def classify_wallet(ref: str) -> str | None:
    """Return the chain for a wallet-shaped reference, else None."""
    ref = ref.strip()
    if _ETH_ADDRESS_RE.match(ref) or ref.lower().endswith(".eth"):
        return "ethereum"
    if _TEZOS_ADDRESS_RE.match(ref) or ref.lower().endswith(".tez"):
        return "tezos"
    return None


def get_job(job_id: str) -> SeedJob | None:
    return _JOBS.get(job_id)


def list_jobs() -> list[SeedJob]:
    return list(_JOBS.values())


class TooManySeedJobs(Exception):
    """Raised when the active-job cap is reached."""


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

        async def created(
            address: str, settings: Settings, client: httpx.AsyncClient
        ) -> AsyncIterator[dict[str, Any]]:
            async for item in _tezos_created_items(address, settings, client, include_burned):
                yield item

        return created
    if scope == "contract":
        return _eth_contract_items if chain == "ethereum" else _tezos_contract_items
    return _eth_items if chain == "ethereum" else _tezos_items


async def start_seed(
    ref: str,
    settings: Settings,
    client: httpx.AsyncClient,
    limit: int | None = None,
    scope: str = "held",
    include_burned: bool = False,
) -> SeedJob | None:
    """Kick off a background seed of `ref`. None when it isn't wallet-shaped.

    Admission control: jobs coalesce per resolved wallet address — the name
    is resolved up front so `name.eth` and its 0x… address are the same
    wallet, and a running job for it is returned instead of duplicated.
    Resolution failures (ValueError, httpx.HTTPError) therefore surface
    here, not inside the job. At most `seed_max_active` jobs run at once
    (TooManySeedJobs otherwise). Finished-job history is bounded.

    scope="published" seeds the works the wallet first-minted (Tezos only)
    instead of its holdings; scope="created" seeds what the wallet authored
    (creators/authors metadata, Tezos only), dropping fully-burned works
    unless include_burned; scope="contract" seeds every token a token
    contract ever issued (ref must be the literal contract address — the
    contract/account distinction is the caller's assertion). Jobs with
    different scopes for the same address are different jobs and may run
    concurrently.
    """
    ref = ref.strip()
    chain = classify_wallet(ref)
    if chain is None:
        return None
    _check_scope(ref, chain, scope)

    running = [job for job in _JOBS.values() if job.status == "running"]
    for job in running:
        if (
            job.ref == ref
            and job.chain == chain
            and job.scope == scope
            and job.include_burned == include_burned
        ):
            return job  # same spelling — duplicate without resolving

    # Contract refs are already the address to enumerate; a name lookup
    # would answer with an account, not the contract.
    address = ref if scope == "contract" else await _resolve_wallet(ref, chain, settings, client)
    for job in running:
        if (
            job.chain == chain
            and job.scope == scope
            and job.include_burned == include_burned
            and job.address is not None
            and _same_address(chain, job.address, address)
        ):
            return job  # already seeding this wallet under another spelling
    if len(running) >= settings.seed_max_active:
        raise TooManySeedJobs(f"{len(running)} seed jobs already running")

    _evict_finished(settings.seed_jobs_kept)
    job = SeedJob(
        id=uuid.uuid4().hex[:8],
        ref=ref,
        chain=chain,
        scope=scope,
        include_burned=include_burned,
        address=address,
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    _JOBS[job.id] = job
    task = asyncio.create_task(run_seed(job, settings, client, limit=limit))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return job


def _same_address(chain, a, b):
    # EIP-55 checksummed vs lowercase spellings are the same account;
    # Tezos base58 is case-sensitive, so compare it exactly.
    return a.lower() == b.lower() if chain == "ethereum" else a == b


def _evict_finished(keep: int) -> None:
    finished = [job_id for job_id, job in _JOBS.items() if job.status != "running"]
    for job_id in finished[: max(0, len(finished) - keep)]:
        del _JOBS[job_id]


async def run_seed(
    job: SeedJob,
    settings: Settings,
    client: httpx.AsyncClient,
    limit: int | None = None,
) -> None:
    _log.info("seed %s: %s (%s) started", job.id, job.ref, job.chain)
    try:
        await asyncio.wait_for(
            _run_seed_inner(job, settings, client, limit),
            timeout=settings.seed_max_seconds,
        )
        job.status = "done"
    except asyncio.TimeoutError:
        job.status = "failed"
        _note_error(job, f"exceeded the {settings.seed_max_seconds:.0f}s wall-clock cap")
    except Exception as exc:  # a seed job must never take the app down
        job.status = "failed"
        _note_error(job, f"{type(exc).__name__}: {exc}")
        _log.exception("seed %s: failed", job.id)
    finally:
        job.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _log.info(
            "seed %s: %s — tokens=%d refs=%d pinned=%d recovered=%d warmed=%d skipped=%d failed=%d",
            job.id, job.status, job.tokens, job.refs_found,
            job.pinned, job.recovered, job.warmed, job.skipped, job.failed,
        )


async def _run_seed_inner(
    job: SeedJob,
    settings: Settings,
    client: httpx.AsyncClient,
    limit: int | None = None,
) -> None:
    # job.address was resolved by start_seed, before admission control.
    items = _enumerator(job.chain, job.scope, job.include_burned)
    cids: dict[str, list[str]] = {}  # cid -> HTTP source URLs (ordered de-dupe)
    txids: dict[str, None] = {}
    captures: dict[str, None] = {}  # unhashed HTTP media (ordered de-dupe)
    async for item in items(job.address, settings, client):
        job.tokens += 1
        for ref in _media_refs(item):
            job.refs_found += 1
            ipfs = ipfs_parts(ref)
            if ipfs is not None:
                cid, path = ipfs
                sources = cids.setdefault(cid, [])
                # A bare-CID gateway URL is a byte-identical HTTP copy we
                # can recover from if the IPFS fetch fails.
                if not path.strip("/") and ref.startswith(("http://", "https://")) and ref not in sources:
                    sources.append(ref)
                continue
            txid = arweave_txid(ref)
            if txid is not None:
                txids[txid] = None
                continue
            if settings.seed_capture_dir and external_url_ok(ref):
                captures[ref] = None
                continue
            job.skipped += 1
        if limit is not None and job.tokens >= limit:
            break

    already_captured = _captured_sources(settings) if captures else set()
    sem = asyncio.Semaphore(settings.seed_concurrency)
    await asyncio.gather(
        *(_pin_cid(cid, sources, job, settings, client, sem) for cid, sources in cids.items()),
        *(_warm_txid(txid, job, settings, client, sem) for txid in txids),
        *(_capture_url(url, job, settings, client, sem, already_captured) for url in captures),
    )


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


def _note_error(job: SeedJob, message: str) -> None:
    if len(job.errors) < _MAX_ERRORS:
        job.errors.append(message)


async def _pin_cid(
    cid: str,
    sources: list[str],
    job: SeedJob,
    settings: Settings,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
) -> None:
    """`pin add` on the box's Kubo — this fetches the DAG and keeps it.

    When the IPFS fetch fails (providers gone), fall back to recovering the
    bytes from an HTTP copy of the same content.
    """
    async with sem:
        try:
            response = await client.post(
                f"{settings.ipfs_api}/api/v0/pin/add",
                params={"arg": f"/ipfs/{cid}"},
                timeout=settings.seed_pin_timeout,
            )
            response.raise_for_status()
            job.pinned += 1
            return
        except httpx.HTTPError as exc:
            pin_error = f"{type(exc).__name__}: {exc}"

        recovery_sources = _recovery_sources(cid, sources, settings)
        if recovery_sources and await _recover_cid(cid, recovery_sources, job, settings, client):
            job.recovered += 1
            _log.info("seed %s: recovered %s from an HTTP copy", job.id, cid)
            return

        job.failed += 1
        _note_error(job, f"pin {cid}: {pin_error}")
        _log.warning("seed %s: pin %s failed (%s); recovery failed", job.id, cid, pin_error)


def _recovery_sources(cid: str, seen: list[str], settings: Settings) -> list[str]:
    """HTTP copies to try: URLs the metadata itself carried, then public
    gateways — their caches often outlive the original providers."""
    sources = [url for url in seen if external_url_ok(url)]
    for gateway in settings.seed_recovery_gateways:
        url = f"{gateway.rstrip('/')}/{cid}"
        if url not in sources:
            sources.append(url)
    return sources


async def _recover_cid(
    cid: str, sources: list[str], job: SeedJob, settings: Settings, client: httpx.AsyncClient
) -> bool:
    """Reproduce a CID from an HTTP copy of its bytes.

    Fetch the bytes, `add` them unpinned, and only pin when Kubo's hash
    round-trips to the same CID — cryptographic proof the recovery is
    faithful. This codifies the side-door that populated the original
    archive: gateway/source URLs in token metadata often still serve
    content whose IPFS providers have vanished. Single-file CIDs only —
    a directory root can't be reproduced from one HTTP body. Files added
    with a non-default chunker won't round-trip; those stay failed.
    """
    add_params = {"pin": "false"}
    if not cid.startswith("Qm"):
        add_params["cid-version"] = "1"

    for url in sources[:3]:
        try:
            with tempfile.TemporaryFile() as buffer:
                async with client.stream("GET", url, timeout=settings.seed_pin_timeout) as response:
                    response.raise_for_status()
                    size = 0
                    async for chunk in response.aiter_bytes(65536):
                        size += len(chunk)
                        if size > settings.seed_recover_max_bytes:
                            raise ValueError(f"body exceeds {settings.seed_recover_max_bytes} bytes")
                        buffer.write(chunk)
                buffer.seek(0)
                response = await client.post(
                    f"{settings.ipfs_api}/api/v0/add",
                    params=add_params,
                    files={"file": ("recovered", buffer)},
                    timeout=settings.seed_pin_timeout,
                )
            response.raise_for_status()
            added = json.loads(response.text.strip().splitlines()[-1])["Hash"]
        except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as exc:
            _log.warning(
                "seed %s: recover %s from %s errored: %s: %s",
                job.id, cid, url, type(exc).__name__, exc,
            )
            continue

        if added != cid:
            _log.warning(
                "seed %s: recover %s from %s: bytes hash to %s — not the same content",
                job.id, cid, url, added,
            )
            continue

        try:
            response = await client.post(
                f"{settings.ipfs_api}/api/v0/pin/add",
                params={"arg": f"/ipfs/{cid}"},
                timeout=settings.seed_pin_timeout,
            )
            response.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            _log.warning("seed %s: pin after recovery of %s failed: %s", job.id, cid, exc)

    return False


async def _warm_txid(
    txid: str, job: SeedJob, settings: Settings, client: httpx.AsyncClient, sem: asyncio.Semaphore
) -> None:
    """Full streaming GET through the box's ar-io gateway; it caches on read."""
    async with sem:
        try:
            async with client.stream(
                "GET",
                f"{settings.arweave_internal}/{txid}",
                timeout=settings.seed_pin_timeout,
            ) as response:
                response.raise_for_status()
                async for _ in response.aiter_bytes(65536):
                    pass
            job.warmed += 1
            record_warm(txid, settings, why="seed")
        except httpx.HTTPError as exc:
            job.failed += 1
            _note_error(job, f"warm {txid}: {type(exc).__name__}: {exc}")
            _log.warning("seed %s: warm %s failed: %s: %s", job.id, txid, type(exc).__name__, exc)


# --- single-target pinning (favorites, resolve?pin=1) ----------------------
# Resolution alone deliberately never pins — browsing must not grow the
# library. These helpers make ONE resolved target durable when the caller
# declares intent (a favorite, an explicit pin=1). Wallet-wide pinning is a
# seed job, not a loop over these.


async def pin_resolved(
    result: Any, settings: Settings, client: httpx.AsyncClient, why: str = "pin"
) -> str | None:
    """Pin what a Resolved points at (IPFS) or warm it (Arweave: ar-io
    caches on read, a full GET is as durable as a cache tier gets).
    Returns "pinned" | "warmed" | None (unresolved or plain-HTTP target)."""
    if not result.resolved:
        return None
    ipfs = ipfs_parts(result.resolved_url)
    if ipfs is not None:
        cid, path = ipfs
        response = await client.post(
            f"{settings.ipfs_api}/api/v0/pin/add",
            params={"arg": f"/ipfs/{cid}{path}"},
            timeout=settings.seed_pin_timeout,
        )
        response.raise_for_status()
        return "pinned"
    if result.provider == "arweave":
        async with client.stream(
            "GET", result.resolved_url, timeout=settings.seed_pin_timeout
        ) as response:
            response.raise_for_status()
            async for _ in response.aiter_bytes(65536):
                pass
        # The resolved URL is the box gateway's /{txid}[/path]; the ledger
        # tracks the txid — the unit the cache (and /library) checks.
        txid = urlparse(result.resolved_url).path.lstrip("/").partition("/")[0]
        if txid:
            record_warm(txid, settings, why=why)
        return "warmed"
    return None


def pin_in_background(
    result: Any, settings: Settings, client: httpx.AsyncClient, why: str = "pin"
) -> None:
    """Fire-and-forget pin of one resolved target: the request that asked
    for it must return immediately even when the content is cold and large."""
    task = asyncio.create_task(_pin_logged(result, settings, client, why))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def _pin_logged(
    result: Any, settings: Settings, client: httpx.AsyncClient, why: str
) -> None:
    try:
        outcome = await pin_resolved(result, settings, client, why=why)
    except Exception as exc:  # best effort: a failed pin must not crash the loop
        _log.warning(
            "%s pin failed for %s: %s: %s", why, result.original_ref, type(exc).__name__, exc
        )
        return
    if outcome:
        _log.info("%s %s: %s", why, result.original_ref, outcome)


def captures_file(settings: Settings) -> Path:
    """One provenance ledger for everything that enters Kubo without a
    canonical content address: seed captures and operator uploads (store.py)."""
    return Path(settings.seed_capture_dir) / "captures.jsonl"


# File I/O around captures (this read, the tempfile buffers and the jsonl
# append in _recover_cid/_capture_url) is deliberately synchronous inside
# async code: at household scale the blocking is microseconds against
# network-bound work, and async-file machinery isn't worth it. Don't "fix" it.
def _captured_sources(settings: Settings) -> set[str]:
    """Source URLs already captured — capture is once per URL, ever."""
    sources: set[str] = set()
    try:
        with open(captures_file(settings)) as fh:
            for line in fh:
                try:
                    source = json.loads(line)["source"]
                except (ValueError, KeyError, TypeError):
                    continue
                if isinstance(source, str):
                    sources.add(source)
    except OSError:
        pass
    return sources


def warmed_file(settings: Settings) -> Path:
    """Ledger of Arweave txids deliberately warmed through the box's gateway.

    ar-io's cache is evictable and exposes no inventory API, so this file is
    the only record of what the box ever *meant* to hold on the Arweave
    plane; /library re-checks each entry against the gateway's X-Cache."""
    return Path(settings.seed_capture_dir) / "warmed.jsonl"


def record_warm(txid: str, settings: Settings, why: str) -> None:
    """Append a successful warm to the ledger — once per txid, ever.

    No-op when capture is disabled. Same deliberately-synchronous file I/O
    as the captures ledger — see the note above _captured_sources."""
    if not settings.seed_capture_dir:
        return
    if txid in warmed_txids(settings):
        return
    record = {
        "txid": txid,
        "warmed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "why": why,
    }
    path = warmed_file(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(record) + "\n")


def warmed_txids(settings: Settings) -> list[str]:
    """Ordered unique txids from the warm ledger; [] when disabled/missing."""
    if not settings.seed_capture_dir:
        return []
    txids: dict[str, None] = {}  # ordered de-dupe
    try:
        with open(warmed_file(settings)) as fh:
            for line in fh:
                try:
                    txid = json.loads(line)["txid"]
                except (ValueError, KeyError, TypeError):
                    continue
                if isinstance(txid, str):
                    txids[txid] = None
    except OSError:
        pass
    return list(txids)


async def _capture_url(
    url: str,
    job: SeedJob,
    settings: Settings,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    already: set[str],
) -> None:
    """Archive an unhashed HTTP media ref while its URL still answers.

    Bytes go into Kubo (added with CIDv1 and pinned); provenance — source
    URL, capture time, size, sha256, content type, the new CID — is appended
    to captures.jsonl. The captured copy is never served automatically:
    pointing a dead canonical ref at it is an operator decision, made in the
    override registry with status `captured-original`.
    """
    async with sem:
        if url in already:
            job.captured += 1  # idempotent, like re-pinning
            return
        digest = hashlib.sha256()
        size = 0
        try:
            with tempfile.TemporaryFile() as buffer:
                async with client.stream("GET", url, timeout=settings.seed_pin_timeout) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("content-type")
                    async for chunk in response.aiter_bytes(65536):
                        size += len(chunk)
                        if size > settings.seed_recover_max_bytes:
                            raise ValueError(f"body exceeds {settings.seed_recover_max_bytes} bytes")
                        digest.update(chunk)
                        buffer.write(chunk)
                buffer.seek(0)
                response = await client.post(
                    f"{settings.ipfs_api}/api/v0/add",
                    params={"cid-version": "1"},
                    files={"file": ("captured", buffer)},
                    timeout=settings.seed_pin_timeout,
                )
            response.raise_for_status()
            cid = json.loads(response.text.strip().splitlines()[-1])["Hash"]
            record = {
                "source": url,
                "cid": cid,
                "sha256": digest.hexdigest(),
                "bytes": size,
                "content_type": content_type,
                "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "wallet": job.ref,
            }
            path = captures_file(settings)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as fh:
                fh.write(json.dumps(record) + "\n")
        except (httpx.HTTPError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            job.failed += 1
            _note_error(job, f"capture {url}: {type(exc).__name__}: {exc}")
            _log.warning("seed %s: capture %s failed: %s: %s", job.id, url, type(exc).__name__, exc)
            return
        already.add(url)
        job.captured += 1
        _log.info("seed %s: captured %s -> %s (%d bytes)", job.id, url, cid, size)

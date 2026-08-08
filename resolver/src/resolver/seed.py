"""Seed native IPFS references discovered from a wallet.

`POST /seed?ref=<0x…|name.eth|tz1…|name.tez>` enumerates the wallet's NFTs
(wallets.py: Blockscout for Ethereum, TzKT for Tezos — keyless public APIs),
extracts every content-addressed media reference from token metadata, then:

  - IPFS refs    -> `pin add` on the box's Kubo API (fetches and keeps the DAG)
  - Arweave refs -> fetched and verified through Curio's one persistent AR.IO Core
  - plain http   -> copied into and synchronously kept by Curio's static backend

Wallet seeding never moves ordinary HTTP bytes into IPFS.

Seeding is a background job: POST returns 202 with a job id immediately;
poll GET /seed/{id}. Jobs live in memory only — a restart forgets history
(the pins themselves, of course, persist in Kubo).

This endpoint answers "make everything this wallet holds locally servable, now."
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import httpx

from .arweave_cache import keep_arweave
from .config import Settings
from .library import ingest_url, keep_task, record_warm
from .refs import arweave_parts, ipfs_parts
from .resolve import external_url_ok, resolve_ref
from .static_store import StaticStore
from .wallets import (
    _check_scope,
    _enumerator,
    _media_refs,
    _resolve_wallet,
    _same_address,
    classify_wallet,
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
    warmed: int = 0  # same-Core fetch/verification operations
    captured: int = 0  # static HTTP captures (kept for wire compatibility)
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


_JOBS: dict[str, SeedJob] = {}


def get_job(job_id: str) -> SeedJob | None:
    return _JOBS.get(job_id)


def list_jobs() -> list[SeedJob]:
    return list(_JOBS.values())


class TooManySeedJobs(Exception):
    """Raised when the active-job cap is reached."""


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
    keep_task(asyncio.create_task(run_seed(job, settings, client, limit=limit)))
    return job


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
    refs: dict[str, None] = {}
    # Resolve every discovered input before deciding its storage plane. A
    # collection often supplies HTTP/data metadata whose final artifact is
    # IPFS or Arweave; retaining the metadata document is not enough.
    async for item in items(job.address, settings, client):
        job.tokens += 1
        for ref in _media_refs(item):
            job.refs_found += 1
            refs[ref] = None
        if limit is not None and job.tokens >= limit:
            break

    sem = asyncio.Semaphore(settings.seed_concurrency)
    native_refs: set[str] = set()
    native_refs_lock = asyncio.Lock()
    await asyncio.gather(
        *(_keep_ref(ref, job, settings, client, sem, native_refs, native_refs_lock) for ref in refs),
    )


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
            ingested = await ingest_url(client, url, settings, add_params=add_params)
        except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as exc:
            _log.warning(
                "seed %s: recover %s from %s errored: %s: %s",
                job.id, cid, url, type(exc).__name__, exc,
            )
            continue

        if ingested.cid != cid:
            _log.warning(
                "seed %s: recover %s from %s: bytes hash to %s — not the same content",
                job.id, cid, url, ingested.cid,
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
    """Fetch an Arweave transaction through the one Core for diagnostics."""
    async with sem:
        try:
            async with client.stream("GET", f"{settings.arweave_internal}/{txid}", timeout=settings.seed_pin_timeout) as response:
                response.raise_for_status()
                async for _ in response.aiter_bytes(65536):
                    pass
            job.warmed += 1
            record_warm(txid, settings, why="seed")
        except httpx.HTTPError as exc:
            job.failed += 1
            _note_error(job, f"warm {txid}: {type(exc).__name__}: {exc}")


async def _keep_txid(
    txid: str, path: str, job: SeedJob, settings: Settings, client: httpx.AsyncClient, sem: asyncio.Semaphore
) -> None:
    """Explicit seed intent fetches and verifies the same persistent Core."""
    async with sem:
        outcome = await keep_arweave(txid, path, settings, client)
        if outcome == "kept":
            job.warmed += 1
            record_warm(txid, settings, why="seed")
            return
        job.failed += 1
        _note_error(job, f"keep {txid}{path}: same-Core cache verification failed")


async def _keep_ref(
    ref: str, job: SeedJob, settings: Settings, client: httpx.AsyncClient, sem: asyncio.Semaphore,
    native_refs: set[str], native_refs_lock: asyncio.Lock,
) -> None:
    """Keep a discovered reference on the final artifact's native plane."""
    try:
        async with sem:
            result = await resolve_ref(ref, settings, client)
    except (httpx.HTTPError, ValueError) as exc:
        job.failed += 1
        _note_error(job, f"retain {ref}: {type(exc).__name__}: {exc}")
        return
    final_ref = result.final_ref
    ipfs = ipfs_parts(final_ref) if final_ref else None
    arweave = arweave_parts(final_ref) if final_ref else None
    # Runtime HTML needs dependency capture/replay before it can honestly be
    # called kept. Native identity alone is not enough to preserve the work.
    if result.keep_state == "live-dependent":
        job.failed += 1
        _note_error(job, f"retain {ref}: HTML runtime has uncaptured dependencies")
        return
    # A CID pin covers every file below that CID; Arweave paths and static
    # artifacts remain distinct identities.
    native_key = f"ipfs:{ipfs[0]}" if ipfs else final_ref
    async with native_refs_lock:
        if native_key in native_refs:
            job.skipped += 1
            return
        native_refs.add(native_key)
    # A seed's explicit keep intent may make a cold IPFS/Arweave artifact
    # locally servable. This is limited to an already-recognized native final
    # ref; arbitrary HTTP/data bodies never enter Kubo.
    if result.source_kind == "ipfs" and ipfs is not None:
        cid, path = ipfs
        sources = [ref] if not path.strip("/") and ref.startswith(("http://", "https://")) else []
        await _pin_cid(cid, sources, job, settings, client, sem)
        return
    if result.source_kind == "arweave" and arweave is not None:
        await _keep_txid(*arweave, job, settings, client, sem)
        return
    if not result.resolved:
        job.failed += 1
        _note_error(job, f"retain {ref}: final artifact is unavailable")
        return
    if result.source_kind in {"http", "data", "upload"} and "/media/" in result.resolved_url:
        if StaticStore(settings.static_root, settings.static_cache_max_bytes).keep(result.resolved_url.rsplit("/", 1)[-1]):
            job.captured += 1
            return
    job.failed += 1
    _note_error(job, f"retain {ref}: final source has no promotable native artifact")

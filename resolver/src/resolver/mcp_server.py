"""MCP surface for Curio: the same capabilities as the REST API,
exposed as Model Context Protocol tools over streamable HTTP at /mcp.

An agent that is merely *connected* to this box (vs told about it) discovers
resolve/browse/seed as typed tools automatically. The server's instructions
are the same self-served SKILL.md the REST surface exposes at /skill — one
source of truth for how to use Curio.

Tools are hand-curated wrappers over the internals (not generated from the
OpenAPI schema) so names and descriptions are agent-quality.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .config import get_settings
from .favorites import FavoriteError, Favorites, get_favorites, list_resolved
from .health import gateway_health
from .library import library_status as _library_status
from .library import pin_in_background, pin_resolved
from .overrides import OverrideError, OverrideRegistry, get_registry, validate_entry
from .refs import canonical_ref_key
from .resolve import resolve_ref
from .seed import get_job, list_jobs, start_seed
from .static_store import StaticStore
from .wallets import list_wallet_tokens

_SKILL_PATH = Path(__file__).parent / "skill" / "SKILL.md"

mcp = FastMCP(
    "curio",
    instructions=_SKILL_PATH.read_text(),
    stateless_http=True,
    # app.py applies equivalent same-origin Host/Origin validation before
    # this mounted transport. FastMCP's static allow-list cannot represent a
    # request-derived deployment origin, so its localhost-only policy would
    # reject every legitimate reverse-proxy/public Host.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

# The shared AsyncClient is owned by the FastAPI lifespan; it hands us a
# reference at startup.
_client: httpx.AsyncClient | None = None


def set_client(client: httpx.AsyncClient) -> None:
    global _client
    _client = client


def _require_client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("Curio HTTP client not initialized")
    return _client


def _mcp_origin(ctx: Context | None = None) -> str:
    """Use the actual MCP HTTP request, falling back only out of transport."""
    if ctx is not None:
        try:
            request = ctx.request_context.request
            return str(request.base_url).rstrip("/")
        except (AttributeError, ValueError):
            pass  # direct/in-process tool invocation has no HTTP request
    settings = get_settings()
    return settings.public_base_url.rstrip("/") or settings.ipfs_public_base.rstrip("/")


def _promote_static(result) -> bool:
    if result.source_kind not in {"http", "data", "upload"} or "/media/" not in result.resolved_url:
        return False
    return StaticStore(get_settings().static_root).keep(result.resolved_url.rsplit("/", 1)[-1])


def _require_curator(token: str | None) -> None:
    configured = get_settings().curator_token
    if not configured or token != configured:
        raise ValueError("curator authentication required")


@mcp.tool()
async def resolve(
    ref: str, pin: bool = False, curator_token: str | None = None, ctx: Context = None
) -> dict[str, Any]:
    """Resolve any media reference into a request-origin playable URL.

    Accepts ipfs://…, /ipfs/…, any gateway URL, ar://txid[/path],
    arweave.net URLs, a tokenURI (JSON metadata, including on-chain data:
    URIs), a verse.works artwork page, or a direct media URL. Returns
    resolved_url (hand it to renderers EXACTLY as returned — query params
    are functional), playback_method ('play' = static media, 'send' = load
    as a web page), title, and content_type. resolved=false means the ref
    was recognized but unresolvable; don't cast those. substituted=true
    means the canonical content is gone and the operator's override registry
    supplied a replacement — substitution_status is its provenance tier.
    pin=true schedules an IPFS pin in the background; it is not completion
    evidence. AR.IO r81 selected-object retention is unsupported, so Arweave
    results report that state rather than pretending a cache warm is durable.
    """
    result = await resolve_ref(ref, get_settings(), _require_client(), origin=_mcp_origin(ctx))
    payload = result.as_dict()
    if pin:
        _require_curator(curator_token)
        if result.source_kind in {"http", "data", "upload"}:
            promoted = result.keep_state != "live-dependent" and _promote_static(result)
            payload["pin_scheduled"] = False
            payload["promoted"] = promoted
            if promoted:
                payload["keep_state"] = "kept"
            elif result.keep_state != "live-dependent":
                payload["keep_state"] = "failed"
        elif result.resolved and result.source_kind == "ipfs":
            pin_in_background(result, get_settings(), _require_client(), why="resolve pin")
            payload["pin_scheduled"] = True
            payload["keep_state"] = "pending"
        elif result.resolved and result.source_kind == "arweave":
            payload["pin_scheduled"] = False
            payload["keep_state"] = (await pin_resolved(result, get_settings(), _require_client(), why="resolve keep")) or "failed"
        else:
            payload["pin_scheduled"] = False
    return payload


@mcp.tool()
async def wallet_tokens(
    ref: str,
    limit: int | None = None,
    pin: bool = False,
    scope: str = "held",
    status: bool = False,
    include_burned: bool = False,
    curator_token: str | None = None,
) -> dict[str, Any]:
    """List a wallet's NFTs live from the public indexers (browse/pick step).

    Call this to choose something to display: ref is 0x…, name.eth, tz1…, or
    name.tez. Each token carries name, contract, token_id, mime, refs, and
    primary_ref — pass primary_ref to the resolve tool to get a playable URL.
    pin=true additionally makes everything listed durable by starting a seed
    job for the wallet (same as seed_wallet, honoring limit and scope) — the
    response gains pin_job; poll it with seed_status. scope="published"
    (Tezos only) lists the works the wallet FIRST-MINTED — its published
    catalog — instead of its holdings. scope="created" (Tezos only) lists
    what the wallet AUTHORED — tokens crediting it in creators/authors
    metadata — the robust authorship index (first-minter over-captures
    collected fxhash editions and under-captures collector-minted editions);
    fully-burned creations are dropped unless include_burned=true, since a
    work burned to nothing was destroyed on purpose. ETH has no keyless
    creator index, so created is Tezos-only. scope="contract" lists every
    token of a token-contract address (ref must be the literal 0x…/KT1…
    contract address, both chains) — the way to sweep a publication contract
    on ETH. status=true is the AUDIT view: every token's primary_ref is
    resolved and classified in place — status is 'ok', 'substituted' (already
    repaired via the override registry), 'unreachable' (dead content),
    'unresolvable', or 'no-ref' — plus a status_counts summary. Use it to
    find rot without a per-token loop; expect the call to take roughly the
    probe timeout when dead refs exist.
    """
    result = await list_wallet_tokens(
        ref, get_settings(), _require_client(),
        limit=limit, scope=scope, status=status, include_burned=include_burned,
    )
    if result is None:
        raise ValueError("not a wallet-shaped reference (want 0x…, name.eth, tz1…, or name.tez)")
    if pin:
        _require_curator(curator_token)
        job = await start_seed(
            ref, get_settings(), _require_client(),
            limit=limit, scope=scope, include_burned=include_burned,
        )
        result["pin_job"] = job.as_dict() if job else None
    return result


@mcp.tool()
async def seed_wallet(
    ref: str, limit: int | None = None, scope: str = "held", include_burned: bool = False,
    curator_token: str | None = None,
) -> dict[str, Any]:
    """Seed the box's caches from a wallet (background job).

    Pins every IPFS ref the wallet's NFTs carry onto the box's Kubo, warms
    the Arweave cache, recovers vanished content from HTTP copies when the
    hash round-trips, and captures unhashed plain-HTTP media with provenance
    (when enabled). Returns the job immediately; poll with seed_status.
    Re-running is safe — pins are idempotent, so a second pass retries only
    the failures. Use limit for a partial/test run. scope="published" (Tezos
    only) seeds the works the wallet FIRST-MINTED rather than its holdings —
    published works you no longer hold are the most rot-prone corner of a
    collection, since holdings-seeding never touches them. scope="created"
    (Tezos only) seeds what the wallet AUTHORED — creators/authors metadata,
    the robust authorship index — dropping fully-burned creations unless
    include_burned=true (they were destroyed on purpose). scope="contract"
    seeds every token of a token-contract address (ref must be the literal
    0x…/KT1… contract address, both chains) — the way to sweep a publication
    contract on ETH, where no keyless creator index exists.
    """
    _require_curator(curator_token)
    job = await start_seed(
        ref, get_settings(), _require_client(),
        limit=limit, scope=scope, include_burned=include_burned,
    )
    if job is None:
        raise ValueError("not a wallet-shaped reference (want 0x…, name.eth, tz1…, or name.tez)")
    return job.as_dict()


@mcp.tool()
async def seed_status(job_id: str | None = None) -> Any:
    """Status of seed jobs. With job_id: that job's counts (tokens, pinned,
    recovered, warmed, captured, skipped, failed, errors). Without: all jobs
    since the service last restarted (jobs are in-memory only)."""
    if job_id:
        job = get_job(job_id)
        if job is None:
            raise ValueError(f"unknown seed job: {job_id}")
        return job.as_dict()
    return [job.as_dict() for job in list_jobs()]


def _require_registry() -> OverrideRegistry:
    settings = get_settings()
    if not settings.overrides_path:
        raise ValueError("override registry disabled on this box (RESOLVER_OVERRIDES_PATH unset)")
    return get_registry(settings.overrides_path)


@mcp.tool()
async def list_overrides() -> dict[str, Any]:
    """List the operator's override registry: dead canonical refs mapped to
    replacement refs, each with a provenance status. Empty until the operator
    records the first exception; everything ordinary resolves without it."""
    entries = [asdict(entry) for entry in _require_registry().entries()]
    return {"count": len(entries), "entries": entries}


@mcp.tool()
async def add_override(
    ref: str,
    replacement: str,
    status: str,
    token: str | None = None,
    source: str | None = None,
    captured: str | None = None,
    note: str | None = None,
    replace: bool = False,
    curator_token: str | None = None,
    ctx: Context = None,
) -> dict[str, Any]:
    """Point a dead canonical ref at replacement content — an operator
    decision, always disclosed in resolve results (substituted=true).

    ref matches ANY spelling of the same content (ipfs://CID, /ipfs/CID,
    gateway URLs; ar://txid, arweave.net URLs). replacement should already be
    pinned on the box — for a local file, upload it first via POST /store
    (REST only; binary doesn't travel over MCP) and use the returned cid as
    ipfs://<cid>. status is the provenance tier: 'canonical-recovered' (bytes
    reproduce the recorded CID), 'captured-original' (fetched from the
    canonical URL while it answered), 'operator-attested' (no hash ever
    existed; operator stands behind the copy), 'alternate-master' (different
    bytes, e.g. a platform HR master). token/source/captured/note are
    provenance metadata — record what you know. An existing entry for the
    same ref errors unless replace=true. The response's replacement_resolved
    tells you whether the replacement actually resolves right now.
    """
    _require_curator(curator_token)
    entry = validate_entry(
        {
            "ref": ref,
            "replacement": replacement,
            "status": status,
            "token": token,
            "source": source,
            "captured": captured,
            "note": note,
        }
    )
    try:
        replaced = _require_registry().upsert(entry, replace=replace)
    except OverrideError as exc:
        raise ValueError(str(exc)) from exc
    try:
        # Disclosure, never a gate: the write already happened.
        check = await resolve_ref(entry.replacement, get_settings(), _require_client(), origin=_mcp_origin(ctx))
    except Exception:
        check = None
    return {
        "entry": asdict(entry),
        "canonical_key": canonical_ref_key(entry.ref),
        "replaced": replaced,
        "replacement_resolved": check.resolved if check else None,
        "replacement_resolved_url": check.resolved_url if check and check.resolved else None,
    }


@mcp.tool()
async def remove_override(ref: str, curator_token: str | None = None) -> dict[str, Any]:
    """Remove the override for a ref (any spelling of it). The dead canonical
    ref goes back to resolving as itself — i.e. failing — so only remove an
    entry when the canonical content is available again or the substitution
    was wrong."""
    _require_curator(curator_token)
    try:
        removed = _require_registry().remove(ref)
    except OverrideError as exc:
        raise ValueError(str(exc)) from exc
    return {"removed": asdict(removed)}


def _require_favorites() -> Favorites:
    settings = get_settings()
    if not settings.favorites_path:
        raise ValueError("favorites disabled on this box (RESOLVER_FAVORITES_PATH unset)")
    return get_favorites(settings.favorites_path)


@mcp.tool()
async def list_favorites(ctx: Context = None) -> dict[str, Any]:
    """The household's favorites: media references the owner marked as picks.

    This is the browse list, resolved and ready to play: each record
    carries ref, title, note, added_at, plus a live resolved_url and
    playback_method — hand resolved_url straight to a renderer, no separate
    resolve call needed. resolved: false marks a favorite whose content is
    currently unreachable."""
    records = await list_resolved(_require_favorites(), get_settings(), _require_client(), _mcp_origin(ctx))
    return {"count": len(records), "favorites": records}


@mcp.tool()
async def add_favorite(
    ref: str, note: str | None = None, curator_token: str | None = None, ctx: Context = None
) -> dict[str, Any]:
    """Mark a media reference as a household favorite.

    ref accepts any spelling of the content (ipfs://CID, /ipfs/CID, gateway
    URLs; ar://txid, arweave.net URLs; or a direct URL) — respellings of the
    same content count as the same favorite, so adding it twice errors. The
    resolver is consulted once to record a title for the browse list
    (enrichment only, never a gate — the response's `resolved` field says
    whether the ref resolves right now). Favoriting also makes the content
    durable where the source supports it: static records are promoted
    synchronously and IPFS pinning is scheduled (`pin_scheduled=true`). AR.IO
    selected-object retention is unsupported and is reported explicitly.
    Removing a
    favorite never unpins. Use note for why it was picked.
    """
    _require_curator(curator_token)
    favorites = _require_favorites()
    try:
        # Enrichment, never a gate: a resolve hiccup must not block the pick.
        check = await resolve_ref(ref, get_settings(), _require_client(), origin=_mcp_origin(ctx))
    except Exception:
        check = None
    try:
        record = favorites.add(ref, title=check.title if check else None, note=note)
    except FavoriteError as exc:
        raise ValueError(str(exc)) from exc
    pin_scheduled = bool(check and check.resolved and check.source_kind not in {"http", "data", "upload", "arweave"})
    promoted = False
    if check and check.resolved and check.source_kind in {"http", "data", "upload"}:
        promoted = check.keep_state != "live-dependent" and _promote_static(check)
        check.keep_state = "kept" if promoted else check.keep_state
    elif pin_scheduled:
        pin_in_background(check, get_settings(), _require_client())
        check.keep_state = "pending"
    elif check and check.resolved and check.source_kind == "arweave":
        check.keep_state = (await pin_resolved(check, get_settings(), _require_client(), why="favorite")) or "failed"
    return {
        **record,
        "resolved": check.resolved if check else None,
        "pin_scheduled": pin_scheduled,
        "promoted": promoted,
        "keep_state": check.keep_state if check else None,
    }


@mcp.tool()
async def remove_favorite(ref: str, curator_token: str | None = None) -> dict[str, Any]:
    """Remove a favorite by its ref (any spelling of the same content
    matches). Only unmarks the pick — nothing is unpinned or deleted."""
    _require_curator(curator_token)
    try:
        removed = _require_favorites().remove(ref)
    except FavoriteError as exc:
        raise ValueError(str(exc)) from exc
    return {"removed": removed}


@mcp.tool()
async def health() -> dict[str, Any]:
    """Reachability of the box's own IPFS and Arweave gateways."""
    return await gateway_health(get_settings(), _require_client())


@mcp.tool()
async def library_status() -> dict[str, Any]:
    """What the box's library actually holds, plane by plane.

    ipfs: `pinned` counts recursive pins on the box's Kubo — the durable
    tier, immune to cache GC — plus the repo's size and object count.
    arweave: `known_warmed` is every txid ever deliberately warmed through
    the box's ar-io gateway (seed jobs);
    `currently_cached` is how many of those the gateway reports as cache
    HITs right now. The ar-io cache is EVICTABLE — warmed content can be
    garbage-collected, and eviction shows as currently_cached <
    known_warmed (a `note` flags it). Re-warm by re-seeding the wallet or
    resolving the ref with pin=true. registry: counts of the operator's
    overrides, favorites, and capture-ledger entries; null means that
    subsystem is disabled on this box. Failures in one plane degrade to an
    `error` field there instead of failing the whole call.
    """
    return await _library_status(get_settings(), _require_client())

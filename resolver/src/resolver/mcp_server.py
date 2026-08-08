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

from . import operations
from .config import get_settings
from .favorites import FavoriteError, Favorites, get_favorites, list_resolved
from .health import gateway_health
from .library import library_status as _library_status
from .origin import effective_origin
from .overrides import OverrideError, OverrideRegistry, get_registry
from .resolve import resolve_ref
from .seed import get_job, list_jobs, start_seed
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
            settings = get_settings()
            origin = effective_origin(request, settings.public_base_url, settings.trusted_proxy_cidrs)
            if origin is None:
                raise ValueError("invalid MCP Host")
            return origin
        except (AttributeError, ValueError):
            # Direct/in-process calls have no request. HTTP MCP traffic is
            # rejected by app.py's pre-route Host guard before this fallback.
            pass
    settings = get_settings()
    return settings.public_base_url.rstrip("/") or settings.ipfs_public_base.rstrip("/")


def _require_curator(token: str | None) -> None:
    configured = get_settings().curator_token
    if not configured or token != configured:
        raise ValueError("curator authentication required")


@mcp.tool()
async def resolve(
    ref: str, pin: bool = False, curator_token: str | None = None, ctx: Context = None
) -> dict[str, Any]:
    """Resolve a reference into a playable URL on the Curio origin.

    Accepts IPFS and Arweave spellings, metadata/data URIs, Verse pages, and
    direct HTTP media. `media_url`/`resolved_url` is ready for a renderer;
    `play` is static media and `send` is HTML. `resolved=false` means Curio
    cannot serve a local artifact. `substituted=true` discloses an override.
    pin=true requires curator_token: IPFS reports a background pending pin,
    static media is promoted synchronously, and Arweave fully fetches then
    verifies the same persistent Core cache. Runtime HTML remains live-dependent.
    """
    settings = get_settings()
    client = _require_client()
    result = await resolve_ref(ref, settings, client, origin=_mcp_origin(ctx))
    if pin:
        _require_curator(curator_token)
        return await operations.resolved_with_optional_pin(result, settings, client)
    return result.as_dict()


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
    """Start a source-appropriate wallet keep job (background).

    It pins final IPFS artifacts, fetches and verifies final Arweave artifacts
    through the one persistent Core, and promotes final HTTP/data artifacts in
    Curio static storage. Ordinary bytes never enter IPFS implicitly. Poll
    with seed_status. Re-running is safe; use limit for a partial run.
    scope="published" and "created" are Tezos-only; "contract" accepts a
    literal 0x…/KT1… contract on either supported chain.
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
    gateway URLs; ar://txid, arweave.net URLs). replacement must already
    resolve through Curio — REST `POST /store` creates kept static media and
    returns its `media_url`; binary upload is not an MCP tool. status is the
    provenance tier: 'canonical-recovered' (bytes
    reproduce the recorded CID), 'captured-original' (fetched from the
    canonical URL while it answered), 'operator-attested' (no hash ever
    existed; operator stands behind the copy), 'alternate-master' (different
    bytes, e.g. a platform HR master). token/source/captured/note are
    provenance metadata — record what you know. An existing entry for the
    same ref errors unless replace=true. The response's replacement_resolved
    tells you whether the replacement actually resolves right now.
    """
    _require_curator(curator_token)
    try:
        return await operations.create_override(
            _require_registry(),
            {
                "ref": ref,
                "replacement": replacement,
                "status": status,
                "token": token,
                "source": source,
                "captured": captured,
                "note": note,
            },
            replace=replace,
            settings=get_settings(),
            client=_require_client(),
            origin=lambda: _mcp_origin(ctx),
        )
    except OverrideError as exc:
        raise ValueError(str(exc)) from exc


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
    synchronously, IPFS pinning is scheduled (`pin_scheduled=true`), and
    Arweave synchronously fetches and verifies the same persistent Core cache. Runtime HTML remains
    live-dependent. Removing a favorite never unpins. Use note for why it
    was picked.
    """
    _require_curator(curator_token)
    try:
        created = await operations.create_favorite(
            _require_favorites(),
            ref,
            note,
            settings=get_settings(),
            client=_require_client(),
            origin=lambda: _mcp_origin(ctx),
        )
    except FavoriteError as exc:
        raise ValueError(str(exc)) from exc
    result, record = created.result, created.record
    return {
        **record,
        "resolved": result.resolved if result else None,
        "final_ref": result.final_ref if result else record.get("final_ref"),
        "source_ref": result.final_ref if result else record.get("final_ref"),
        "pin_scheduled": created.pin_scheduled,
        "promoted": created.promoted,
        "keep_state": result.keep_state if result else None,
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
    """What the box holds, plane by plane.

    `ipfs.pinned` counts recursive Kubo pins. Arweave `known_warmed` and
    `currently_cached` describe the one persistent Core cache. Resolve/play
    also populate it; explicit keep eagerly fetches and verifies a local hit.
    This is not an Arweave-network replication claim. Registry counts cover
    operator state. A failed plane gets its own error
    rather than failing the complete response.
    """
    return await _library_status(get_settings(), _require_client())

from __future__ import annotations

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
from .seed import get_job, list_jobs, start_seed
from .wallets import list_wallet_tokens

_INSTRUCTIONS = (
    "Curio stores NFT media references and local files, then serves them "
    "from one origin. It has no user authentication: it is intended for a "
    "trusted household or studio network and must not be exposed directly "
    "to the public internet. Binary media upload is REST-only (multipart "
    "POST /resolve); media bytes are served on REST routes /media, /ipfs, "
    "/arweave."
)

mcp = FastMCP(
    "curio",
    instructions=_INSTRUCTIONS,
    stateless_http=True,
    # app.py validates request-derived origins before this mounted transport.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

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
            # HTTP traffic is rejected by app.py's Host guard before fallback.
            pass
    settings = get_settings()
    return settings.public_base_url.rstrip("/") or settings.ipfs_public_base.rstrip("/")


@mcp.tool()
async def resolve(ref: str, ctx: Context = None) -> dict[str, Any]:
    """Resolve and store an IPFS, Arweave, HTTP, `data:` metadata or media,
    or Verse reference (artwork page or /items/ URL).

    Both Verse URL forms resolve chain-first. An artwork page
    (verse.works/artworks/<id>) has its contract address and token id read
    from the page; an /items/ethereum/<contract>/<tokenId> URL already names
    them and skips page scraping entirely. Either way, a tokenURI/uri chain
    call fetches the canonical metadata and its media is resolved
    recursively. Only when on-chain resolution is impossible (no
    coordinates, RPC disabled or unreachable, chain metadata unreachable)
    does it fall back to scraping the page directly — and a
    chain-found-but-dead canonical ref is disclosed in `note` even when a
    scrape fallback plays instead.

    A successful response has status `ready` or `live-dependent`, plus the
    reference to pass to `lookup` or HTTP `GET /resolve?ref=...`. `failed`
    means Curio did not register the reference. `live-dependent` means the
    stored primary HTML artifact still relies on network resources Curio has
    not captured — never call such a runtime complete.
    """
    payload, _ = await operations.store_reference(
        ref, get_settings(), _require_client(), _mcp_origin(ctx)
    )
    return payload


@mcp.tool()
async def lookup(ref: str) -> dict[str, Any]:
    """Look up a reference Curio already stored, without submitting it.

    Unknown references answer {"found": false}; lookup never stores new
    media (use resolve for that). A found record's `playable` is false when
    the stored resolution failed, with `reason` explaining why. `media_type`
    tells you how to play it: `text/html` should be sent as a page
    (playback_method `send`); anything else plays directly (`play`).
    """
    record = operations.lookup_resolution(ref, get_settings())
    if record is None:
        return {"found": False}
    return {
        "found": True,
        "playable": record["playable"],
        "reason": record.get("reason"),
        "media_path": record["media_path"],
        "status": record["status"],
        "media_type": record.get("media_type"),
    }


@mcp.tool()
async def wallet_tokens(
    ref: str,
    limit: int | None = None,
    scope: str = "held",
    status: bool = False,
    include_burned: bool = False,
) -> dict[str, Any]:
    """List a wallet's NFTs live from the public indexers (browse/pick step).

    Call this to choose something to display: ref is 0x…, name.eth, tz1…, or
    name.tez. Each token carries name, contract, token_id, mime, refs, and
    primary_ref — pass primary_ref to the resolve tool to get a playable URL.
    Use seed_wallet to store every listed work. scope="published" (Tezos
    only) lists the works the wallet FIRST-MINTED — its published
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
    return result


@mcp.tool()
async def seed_wallet(
    ref: str, limit: int | None = None, scope: str = "held", include_burned: bool = False,
) -> dict[str, Any]:
    """Start a source-appropriate wallet storage job (background).

    It pins final IPFS artifacts, fetches final Arweave artifacts through the
    persistent Core, and stores final HTTP/data artifacts in Curio. Ordinary
    bytes never enter IPFS implicitly. Poll
    with seed_status. Re-running is safe; use limit for a partial run.
    scope="published" and "created" are Tezos-only; "contract" accepts a
    literal Ethereum or Tezos token-contract address on either supported
    chain. Fully burned authored works are omitted unless include_burned=true.
    """
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
async def list_overrides(raw: bool = False) -> dict[str, Any]:
    """List the operator's override registry: dead canonical refs mapped to
    replacement refs, each disclosing a provenance status
    (canonical-recovered, captured-original, operator-attested, or
    alternate-master). Empty until the operator records the first exception;
    everything ordinary resolves without it. raw=true returns the registry
    file verbatim (TOML) under "raw", for snapshots — matches REST
    `GET /override?raw=1`."""
    registry = _require_registry()
    if raw:
        try:
            return {"raw": registry.raw_text()}
        except OverrideError as exc:
            raise ValueError(str(exc)) from exc
    return operations.override_listing(registry)


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
    ctx: Context = None,
) -> dict[str, Any]:
    """Point a dead canonical ref at replacement content — an operator
    decision, always disclosed in resolve results (substituted=true).

    ref matches ANY spelling of the same content (ipfs://CID, /ipfs/CID,
    gateway URLs; ar://txid, arweave.net URLs). replacement must already
    resolve through Curio — a multipart `POST /resolve` stores an uploaded
    replacement; binary upload is not an MCP tool. status is the
    provenance tier: 'canonical-recovered' (bytes
    reproduce the recorded CID), 'captured-original' (fetched from the
    canonical URL while it answered), 'operator-attested' (no hash ever
    existed; operator stands behind the copy), 'alternate-master' (different
    bytes, e.g. a platform HR master). token/source/captured/note are
    provenance metadata — record what you know. An existing entry for the
    same ref errors unless replace=true. The response's replacement_resolved
    tells you whether the replacement actually resolves right now.
    """
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
async def remove_override(ref: str) -> dict[str, Any]:
    """Remove the override for a ref (any spelling of it). The dead canonical
    ref goes back to resolving as itself — i.e. failing — so only remove an
    entry when the canonical content is available again or the substitution
    was wrong."""
    try:
        removed = _require_registry().remove(ref)
    except OverrideError as exc:
        raise ValueError(str(exc)) from exc
    return operations.override_removed(removed)


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
    ref: str, note: str | None = None, ctx: Context = None
) -> dict[str, Any]:
    """Mark a media reference as a household favorite.

    ref accepts any spelling of the content (ipfs://CID, /ipfs/CID, gateway
    URLs; ar://txid, arweave.net URLs; or a direct URL) — respellings of the
    same content count as the same favorite, so adding it twice errors. The
    resolver is consulted once to record a title for the browse list
    (enrichment only, never a gate — the response's `resolved` field says
    whether the ref resolves right now). Favorites organize the library; use
    POST /resolve or seed_wallet to store media. Use note for why it was picked.
    """
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
    return created.response()


@mcp.tool()
async def remove_favorite(ref: str) -> dict[str, Any]:
    """Remove a favorite by its ref (any spelling of the same content
    matches). Only unmarks the pick — nothing is unpinned or deleted."""
    try:
        removed = _require_favorites().remove(ref)
    except FavoriteError as exc:
        raise ValueError(str(exc)) from exc
    return operations.favorite_removed(removed)


@mcp.tool()
async def dp1_playlist(
    refs: list[str],
    title: str | None = None,
    duration: int | None = None,
    ctx: Context = None,
) -> dict[str, Any]:
    """Emit an unsigned DP-1 1.0.0 playlist for catalogued works.

    DP-1 is the display protocol spoken by DP-1 players (e.g. the Feral File
    FF1). Time-based media (video/audio) gets display.loop=true and a long
    duration so a single work loops natively instead of ending and
    triggering a playlist-advance reload. Sign and play the result with the
    operator's DP-1 tooling (e.g. `ff-cli validate`, `ff-cli sign`, `ff-cli
    play`) — Curio never signs a playlist or talks to a device. Every ref
    must already be catalogued and playable (resolve it first); an unknown
    or failed ref raises rather than being silently dropped from the
    playlist.
    """
    return operations.dp1_playlist(
        refs, get_settings(), _mcp_origin(ctx), title=title, duration=duration
    )


@mcp.tool()
async def health() -> dict[str, Any]:
    """Reachability of the box's own IPFS and Arweave gateways."""
    return await gateway_health(get_settings(), _require_client())


@mcp.tool()
async def library_status() -> dict[str, Any]:
    """What the box holds, plane by plane.

    `ipfs.pinned` counts recursive Kubo pins. Arweave `known_warmed` and
    `currently_cached` describe the one persistent Core cache. Resolution and
    playback populate that same Core.
    This is not an Arweave-network replication claim. Registry counts cover
    operator state. A failed plane gets its own error
    rather than failing the complete response.
    """
    return await _library_status(get_settings(), _require_client())

"""HTTP surface for the resolver.

  GET  /resolve?ref=<anything>  -> JSON {resolved_url, playback_method, ...}
  GET  /c?ref=<anything>        -> 302 to the resolved URL (for dumb renderers)
  GET  /wallet?ref=<wallet|name> -> live normalized NFT inventory (browse/pick)
  POST /seed?ref=<wallet|name>  -> 202 + job; pin/warm everything the wallet holds
  GET  /seed, /seed/{id}        -> seed job status
  GET  /override[?raw=1]        -> the operator's exception registry (JSON | TOML)
  POST /override                -> add/replace an override (JSON body)
  DELETE /override?ref=         -> remove an override
  GET  /favorites                -> the household's favorites (browse/pick)
  POST /favorites?ref=&note=     -> add a favorite (any spelling of the ref)
  DELETE /favorites?ref=         -> remove a favorite
  POST /store                   -> multipart upload -> Kubo (pinned) + provenance
  GET  /library                 -> cross-plane library status (pins, warm cache, registry)
  GET  /skill[/<name>]          -> agent instructions + shipped skills, self-served
  GET  /healthz                 -> on-box gateway reachability
  /mcp                          -> MCP (streamable HTTP): same capabilities as tools

Schema docs are FastAPI's stock /docs + /openapi.json.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from importlib import metadata
from pathlib import Path

import httpx
from fastapi import FastAPI, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

from .config import get_settings
from .favorites import (
    DuplicateFavorite,
    FavoriteNotFound,
    Favorites,
    FavoritesUnparseable,
    get_favorites,
    list_resolved,
)
from .health import gateway_health, library_status
from .mcp_server import mcp, set_client
from .overrides import (
    DuplicateOverride,
    OverrideNotFound,
    OverrideRegistry,
    RegistryUnparseable,
    get_registry,
    validate_entry,
)
from .refs import canonical_ref_key
from .resolve import resolve_ref
from .seed import (
    TooManySeedJobs,
    get_job,
    list_jobs,
    list_wallet_tokens,
    pin_in_background,
    start_seed,
)
from .store import CidMismatch, store_upload


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = httpx.AsyncClient(
        follow_redirects=True, timeout=get_settings().http_timeout
    )
    set_client(app.state.client)
    # A mounted sub-app's own lifespan never runs; the MCP session manager
    # must be driven from the host lifespan.
    async with mcp.session_manager.run():
        try:
            yield
        finally:
            await app.state.client.aclose()


app = FastAPI(title="content-resolver", lifespan=lifespan)


@app.get("/resolve")
async def resolve(
    ref: str = Query(..., description="Any media reference"),
    pin: bool = Query(False, description="Also pin/warm the resolved content (background)"),
):
    result = await resolve_ref(ref, get_settings(), app.state.client)
    payload = result.as_dict()
    if pin:
        # Opt-in only: plain resolution never pins (browsing must not grow
        # the library); pin=1 is the caller declaring keep-this intent.
        if result.resolved:
            pin_in_background(result, get_settings(), app.state.client, why="resolve pin")
        payload["pin_scheduled"] = result.resolved
    return JSONResponse(payload)


@app.get("/c")
async def cast(ref: str = Query(..., description="Any media reference")):
    result = await resolve_ref(ref, get_settings(), app.state.client)
    if not result.resolved:
        # Redirecting a renderer at an unresolved ref would point it at
        # garbage or at the metadata document itself.
        return JSONResponse(result.as_dict(), status_code=422)
    return RedirectResponse(result.resolved_url, status_code=302)


def _wallet_error(exc: Exception) -> JSONResponse:
    # A bad scope is the caller's mistake (400); everything else raised here
    # — name resolution, indexer trouble — is an upstream failure (502).
    caller_mistake = isinstance(exc, ValueError) and "scope" in str(exc)
    return JSONResponse(
        {"error": f"{type(exc).__name__}: {exc}"},
        status_code=400 if caller_mistake else 502,
    )


@app.get("/wallet")
async def wallet(
    ref: str = Query(..., description="Wallet address or name: 0x…, name.eth, tz1…, name.tez"),
    limit: int | None = Query(None, ge=1, description="Stop after this many tokens"),
    pin: bool = Query(False, description="Also pin everything listed (starts a seed job)"),
    scope: str = Query("held", description="'held' = holdings; 'published' = works the wallet first-minted (Tezos only); 'created' = works the wallet authored, i.e. creators/authors metadata, fully-burned dropped (Tezos only); 'contract' = every token of a token-contract address (both chains)"),
    status: bool = Query(False, description="Also resolve each primary_ref and classify it (ok/substituted/unreachable/unresolvable/no-ref) — the audit view"),
    include_burned: bool = Query(False, description="created scope only: keep fully-burned creations (default drops them — destroyed on purpose)"),
):
    try:
        result = await list_wallet_tokens(
            ref, get_settings(), app.state.client,
            limit=limit, scope=scope, status=status, include_burned=include_burned,
        )
    except (httpx.HTTPError, ValueError) as exc:
        return _wallet_error(exc)
    if result is None:
        return JSONResponse(
            {"error": "not a wallet-shaped reference (want 0x…, name.eth, tz1…, or name.tez)"},
            status_code=400,
        )
    if pin:
        # Pinning a wallet's holdings IS a seed job — reuse its admission
        # control, recovery, and capture instead of a bare pin loop. The
        # browse result still returns even when the job is refused.
        try:
            job = await start_seed(
                ref, get_settings(), app.state.client,
                limit=limit, scope=scope, include_burned=include_burned,
            )
            result["pin_job"] = job.as_dict() if job else None
        except (TooManySeedJobs, httpx.HTTPError, ValueError) as exc:
            result["pin_job"] = None
            result["pin_error"] = f"{type(exc).__name__}: {exc}"
    return JSONResponse(result)


@app.post("/seed")
async def seed(
    ref: str = Query(..., description="Wallet address or name: 0x…, name.eth, tz1…, name.tez"),
    limit: int | None = Query(None, ge=1, description="Stop after this many tokens (for testing/incremental runs)"),
    scope: str = Query("held", description="'held' = holdings; 'published' = works the wallet first-minted (Tezos only); 'created' = works the wallet authored, i.e. creators/authors metadata, fully-burned dropped (Tezos only); 'contract' = every token of a token-contract address (both chains)"),
    include_burned: bool = Query(False, description="created scope only: keep fully-burned creations (default drops them — destroyed on purpose)"),
):
    try:
        job = await start_seed(
            ref, get_settings(), app.state.client,
            limit=limit, scope=scope, include_burned=include_burned,
        )
    except TooManySeedJobs as exc:
        return JSONResponse({"error": str(exc)}, status_code=429)
    except (httpx.HTTPError, ValueError) as exc:
        return _wallet_error(exc)
    if job is None:
        return JSONResponse(
            {"error": "not a wallet-shaped reference (want 0x…, name.eth, tz1…, or name.tez)"},
            status_code=400,
        )
    return JSONResponse(job.as_dict(), status_code=202)


@app.get("/seed")
async def seed_jobs():
    return JSONResponse([job.as_dict() for job in list_jobs()])


@app.get("/seed/{job_id}")
async def seed_job(job_id: str):
    job = get_job(job_id)
    if job is None:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return JSONResponse(job.as_dict())


class OverrideBody(BaseModel):
    """POST /override input. `status` stays a plain str on purpose: validation
    runs through overrides.validate_entry so errors keep the house
    `{"error": …}` shape instead of FastAPI's 422 envelope."""

    ref: str
    replacement: str
    status: str
    token: str | None = None
    source: str | None = None
    captured: str | None = None
    note: str | None = None
    replace: bool = False


def _registry() -> OverrideRegistry | None:
    settings = get_settings()
    return get_registry(settings.overrides_path) if settings.overrides_path else None


def _registry_disabled() -> JSONResponse:
    return JSONResponse(
        {"error": "override registry disabled: set RESOLVER_OVERRIDES_PATH"},
        status_code=503,
    )


@app.get("/override")
async def override_list(
    raw: bool = Query(False, description="Return the registry file verbatim (TOML), for snapshots"),
):
    registry = _registry()
    if registry is None:
        return _registry_disabled()
    if raw:
        try:
            return PlainTextResponse(registry.raw_text())
        except OverrideNotFound as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
    entries = [asdict(entry) for entry in registry.entries()]
    return JSONResponse({"count": len(entries), "entries": entries})


@app.post("/override")
async def override_add(body: OverrideBody):
    registry = _registry()
    if registry is None:
        return _registry_disabled()
    try:
        entry = validate_entry(body.model_dump(exclude={"replace"}))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    try:
        replaced = registry.upsert(entry, replace=body.replace)
    except (DuplicateOverride, RegistryUnparseable) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    try:
        # Disclosure, never a gate: the write above already happened, and a
        # network hiccup must not make it look like it didn't.
        check = await resolve_ref(entry.replacement, get_settings(), app.state.client)
    except Exception:
        check = None
    return JSONResponse(
        {
            "entry": asdict(entry),
            "canonical_key": canonical_ref_key(entry.ref),
            "replaced": replaced,
            "replacement_resolved": check.resolved if check else None,
            "replacement_resolved_url": check.resolved_url if check and check.resolved else None,
        },
        status_code=201,
    )


@app.delete("/override")
async def override_remove(
    ref: str = Query(..., description="Any spelling of the dead canonical ref"),
):
    registry = _registry()
    if registry is None:
        return _registry_disabled()
    try:
        removed = registry.remove(ref)
    except OverrideNotFound as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except RegistryUnparseable as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse({"removed": asdict(removed)})


def _favorites_store() -> Favorites | None:
    settings = get_settings()
    return get_favorites(settings.favorites_path) if settings.favorites_path else None


def _favorites_disabled() -> JSONResponse:
    return JSONResponse(
        {"error": "favorites disabled: set RESOLVER_FAVORITES_PATH"},
        status_code=503,
    )


@app.get("/favorites")
async def favorite_list():
    favorites = _favorites_store()
    if favorites is None:
        return _favorites_disabled()
    records = await list_resolved(favorites, get_settings(), app.state.client)
    return JSONResponse({"count": len(records), "favorites": records})


@app.post("/favorites")
async def favorite_add(
    ref: str = Query(..., description="Any media reference (any spelling of it)"),
    note: str | None = Query(None, description="Optional short note"),
):
    favorites = _favorites_store()
    if favorites is None:
        return _favorites_disabled()
    try:
        # Enrichment, never a gate: a title is nice to have in the browse
        # list, but a network hiccup must not block recording the pick.
        check = await resolve_ref(ref, get_settings(), app.state.client)
    except Exception:
        check = None
    try:
        record = favorites.add(ref, title=check.title if check else None, note=note)
    except (DuplicateFavorite, FavoritesUnparseable) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    pin_scheduled = bool(check and check.resolved)
    if pin_scheduled:
        # A favorite is a keep-this signal: pin/warm its content durably,
        # in the background — cold, large media must not block the POST.
        pin_in_background(check, get_settings(), app.state.client, why="favorite")
    return JSONResponse(
        {
            **record,
            "resolved": check.resolved if check else None,
            "resolved_url": check.resolved_url if check and check.resolved else None,
            "playback_method": check.playback_method if check else None,
            "pin_scheduled": pin_scheduled,
        },
        status_code=201,
    )


@app.delete("/favorites")
async def favorite_remove(
    ref: str = Query(..., description="Any spelling of the favorite's ref"),
):
    favorites = _favorites_store()
    if favorites is None:
        return _favorites_disabled()
    try:
        removed = favorites.remove(ref)
    except FavoriteNotFound as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except FavoritesUnparseable as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse({"removed": removed})


@app.post("/store")
async def store(
    file: UploadFile,
    expect_cid: str | None = Query(
        None,
        description="Canonical recovery: pin only if the bytes reproduce this CID (409 otherwise)",
    ),
):
    """Upload a local file into the box's Kubo (pinned) with provenance
    recorded — the supply side of an override's replacement ref."""
    settings = get_settings()
    if not settings.seed_capture_dir:
        return JSONResponse(
            {"error": "store disabled: set RESOLVER_SEED_CAPTURE_DIR (the provenance ledger)"},
            status_code=503,
        )
    try:
        result = await store_upload(file, settings, app.state.client, expect_cid=expect_cid)
    except CidMismatch as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=413)
    except (httpx.HTTPError, KeyError, json.JSONDecodeError, OSError) as exc:
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=502)
    return JSONResponse(result, status_code=201)


@app.get("/library")
async def library():
    """What the box actually holds, plane by plane: IPFS pins (durable),
    warmed Arweave txids live-checked against the evictable cache, and the
    operator-state counts."""
    return JSONResponse(await library_status(get_settings(), app.state.client))


_SKILL_DIR = Path(__file__).parent / "skill"
# Whitelist built at import — the API skill plus any skills shipped inside
# the package (skill/<name>/SKILL.md). Serving only dict members (never the
# raw request path) forecloses path traversal.
_SKILL_FILES = {
    "SKILL.md": _SKILL_DIR / "SKILL.md",
    **{f"{p.parent.name}/SKILL.md": p for p in sorted(_SKILL_DIR.glob("*/SKILL.md"))},
}


@app.get("/skill")
async def skill():
    """Agent instructions for this service, served by the service itself."""
    return FileResponse(_SKILL_FILES["SKILL.md"], media_type="text/markdown")


@app.get("/skill/{name:path}")
async def skill_file(name: str):
    """Skills shipped with the service — e.g. /skill/nft-preservation.
    No ecosystem convention for distributing skills exists yet; this box's
    convention is that it serves its own."""
    path = _SKILL_FILES.get(name) or _SKILL_FILES.get(f"{name}/SKILL.md")
    if path is None:
        return JSONResponse(
            {"error": "unknown skill", "available": sorted(_SKILL_FILES)},
            status_code=404,
        )
    return FileResponse(path, media_type="text/markdown")


@app.get("/healthz")
async def healthz():
    try:
        version = metadata.version("content-resolver")
    except metadata.PackageNotFoundError:
        version = "unknown"
    result = await gateway_health(get_settings(), app.state.client)
    result["version"] = version
    return JSONResponse(result, status_code=200 if result["healthy"] else 503)


# Mounted last so explicit routes win; the MCP app serves /mcp and 404s the rest.
app.mount("/", mcp.streamable_http_app())


def main() -> None:
    import logging

    import uvicorn

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port)

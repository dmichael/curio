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
  POST /store                   -> multipart upload -> Curio static storage + provenance
  GET  /library                 -> cross-plane library status (pins, warm cache, registry)
  GET  /skill[/<name>]          -> agent instructions + shipped skills, self-served
  GET  /healthz                 -> on-box gateway reachability
  /mcp                          -> MCP (streamable HTTP): same capabilities as tools

Schema docs are FastAPI's stock /docs + /openapi.json.
"""

from __future__ import annotations

import tempfile
from contextlib import asynccontextmanager
from dataclasses import asdict
from importlib import metadata
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel
from starlette.background import BackgroundTask

from .arweave_retention import retained_state
from .config import get_settings
from .favorites import (
    DuplicateFavorite,
    FavoriteNotFound,
    Favorites,
    FavoritesUnparseable,
    get_favorites,
    list_resolved,
)
from .health import gateway_health
from .library import library_status, pin_in_background, pin_resolved
from .mcp_server import mcp, set_client
from .overrides import (
    DuplicateOverride,
    OverrideNotFound,
    OverrideRegistry,
    RegistryUnparseable,
    get_registry,
    validate_entry,
)
from .refs import arweave_parts, canonical_ref_key
from .resolve import resolve_ref
from .seed import TooManySeedJobs, get_job, list_jobs, start_seed
from .static_store import StaticStore
from .wallets import list_wallet_tokens


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Redirects are deliberately handled by source adapters; an HTTP client's
    # implicit redirect would bypass per-target SSRF validation.
    app.state.client = httpx.AsyncClient(
        follow_redirects=False, timeout=get_settings().http_timeout
    )
    set_client(app.state.client)
    # A mounted sub-app's own lifespan never runs; the MCP session manager
    # must be driven from the host lifespan.
    async with mcp.session_manager.run():
        try:
            yield
        finally:
            await app.state.client.aclose()


app = FastAPI(title="Curio", lifespan=lifespan)


@app.middleware("http")
async def mcp_same_origin_guard(request: Request, call_next):
    """DNS-rebinding protection for MCP with request-derived public origins.

    FastMCP's built-in allow-list is static and only knows localhost. Curio
    derives its public origin from this request, so require a syntactically
    safe Host and, when a browser sends Origin, require that exact origin.
    This runs before the mounted MCP transport.
    """
    if request.url.path.startswith("/mcp"):
        host = request.headers.get("host", "")
        if not host or any(c in host for c in "\r\n/@\\") or host.startswith("."):
            return JSONResponse({"error": "invalid MCP Host"}, status_code=421)
        origin = request.headers.get("origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.scheme not in {"http", "https"} or parsed.netloc != host or parsed.path not in {"", "/"}:
                return JSONResponse({"error": "invalid MCP Origin"}, status_code=403)
    return await call_next(request)


def request_origin(request: Request) -> str:
    """The public front door, never an untrusted forwarded header or Docker URL."""
    configured = get_settings().public_base_url.rstrip("/")
    return configured or str(request.base_url).rstrip("/")


def _promote_static(result) -> bool:
    """Promote the source-native static object; never route HTTP/data via IPFS."""
    if result.source_kind not in {"http", "data", "upload"} or "/media/" not in result.resolved_url:
        return False
    return StaticStore(get_settings().static_root).keep(result.resolved_url.rsplit("/", 1)[-1])


def require_curator(authorization: str | None = Header(default=None)) -> None:
    token = get_settings().curator_token
    if not token:
        raise HTTPException(503, "curator mutations are disabled: set RESOLVER_CURATOR_TOKEN")
    if authorization != f"Bearer {token}":
        raise HTTPException(401, "curator authentication required")

_SCOPE_DESCRIPTION = (
    "'held' = holdings; 'published' = works the wallet first-minted (Tezos only); "
    "'created' = works the wallet authored, i.e. creators/authors metadata, fully-burned "
    "dropped (Tezos only); 'contract' = every token of a token-contract address (both chains)"
)


@app.get("/resolve")
async def resolve(
    request: Request,
    ref: str = Query(..., description="Any media reference"),
    pin: bool = Query(False, description="Also pin/warm the resolved content (background)"),
):
    if pin:
        require_curator(request.headers.get("authorization"))
    result = await resolve_ref(ref, get_settings(), app.state.client, origin=request_origin(request))
    payload = result.as_dict()
    if pin:
        # Static objects have a local durable store, so promote them in this
        # request and never pretend a no-op IPFS helper was scheduled.
        if result.source_kind in {"http", "data", "upload"}:
            promoted = result.keep_state != "live-dependent" and _promote_static(result)
            payload["pin_scheduled"] = False
            payload["promoted"] = promoted
            if promoted:
                payload["keep_state"] = "kept"
            elif result.keep_state != "live-dependent":
                payload["keep_state"] = "failed"
        elif result.resolved and result.keep_state != "live-dependent" and result.source_kind == "ipfs":
            pin_in_background(result, get_settings(), app.state.client, why="resolve pin")
            payload["pin_scheduled"] = True
            payload["keep_state"] = "pending"
        elif result.resolved and result.keep_state != "live-dependent" and result.source_kind == "arweave":
            payload["pin_scheduled"] = False
            outcome = await pin_resolved(result, get_settings(), app.state.client, why="resolve keep")
            payload["keep_state"] = outcome or "failed"
        else:
            payload["pin_scheduled"] = False
    return JSONResponse(payload)


@app.get("/c")
async def cast(request: Request, ref: str = Query(..., description="Any media reference")):
    result = await resolve_ref(ref, get_settings(), app.state.client, origin=request_origin(request))
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
    request: Request,
    ref: str = Query(..., description="Wallet address or name: 0x…, name.eth, tz1…, name.tez"),
    limit: int | None = Query(None, ge=1, description="Stop after this many tokens"),
    pin: bool = Query(False, description="Also pin everything listed (starts a seed job)"),
    scope: str = Query("held", description=_SCOPE_DESCRIPTION),
    status: bool = Query(False, description="Also resolve each primary_ref and classify it (ok/substituted/unreachable/unresolvable/no-ref) — the audit view"),
    include_burned: bool = Query(False, description="created scope only: keep fully-burned creations (default drops them — destroyed on purpose)"),
):
    if pin:
        require_curator(request.headers.get("authorization"))
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
    _: None = Depends(require_curator),
    ref: str = Query(..., description="Wallet address or name: 0x…, name.eth, tz1…, name.tez"),
    limit: int | None = Query(None, ge=1, description="Stop after this many tokens (for testing/incremental runs)"),
    scope: str = Query("held", description=_SCOPE_DESCRIPTION),
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
async def override_add(
    request: Request, body: OverrideBody, _: None = Depends(require_curator)
):
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
        check = await resolve_ref(entry.replacement, get_settings(), app.state.client, origin=request_origin(request))
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
    _: None = Depends(require_curator),
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
async def favorite_list(request: Request):
    favorites = _favorites_store()
    if favorites is None:
        return _favorites_disabled()
    records = await list_resolved(favorites, get_settings(), app.state.client, request_origin(request))
    return JSONResponse({"count": len(records), "favorites": records})


@app.post("/favorites")
async def favorite_add(
    request: Request,
    _: None = Depends(require_curator),
    ref: str = Query(..., description="Any media reference (any spelling of it)"),
    note: str | None = Query(None, description="Optional short note"),
):
    favorites = _favorites_store()
    if favorites is None:
        return _favorites_disabled()
    try:
        # Enrichment, never a gate: a title is nice to have in the browse
        # list, but a network hiccup must not block recording the pick.
        check = await resolve_ref(ref, get_settings(), app.state.client, origin=request_origin(request))
    except Exception:
        check = None
    try:
        record = favorites.add(
            ref, title=check.title if check else None, note=note,
            final_ref=check.final_ref if check else None,
        )
    except (DuplicateFavorite, FavoritesUnparseable) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    pin_scheduled = bool(
        check and check.resolved and check.keep_state != "live-dependent"
        and check.source_kind not in {"http", "data", "upload", "arweave"}
    )
    promoted = False
    if check and check.resolved and check.source_kind in {"http", "data", "upload"}:
        # Static artifacts are kept in their own durable store, not copied to
        # IPFS. HTML is a captured shell with live dependencies, not a kept runtime.
        promoted = check.keep_state != "live-dependent" and _promote_static(check)
        check.keep_state = "kept" if promoted else check.keep_state
    elif pin_scheduled:
        pin_in_background(check, get_settings(), app.state.client, why="favorite")
        check.keep_state = "pending"
    elif check and check.resolved and check.keep_state != "live-dependent" and check.source_kind == "arweave":
        # Favorites are explicit keep intent. This is a private native Core
        # hydration, not a claim that r81 offers a pin endpoint.
        check.keep_state = (await pin_resolved(check, get_settings(), app.state.client, why="favorite")) or "failed"
    return JSONResponse(
        {
            **record,
            "resolved": check.resolved if check else None,
            "resolved_url": check.resolved_url if check and check.resolved else None,
            "playback_method": check.playback_method if check else None,
            "final_ref": check.final_ref if check else record.get("final_ref"),
            "source_ref": check.final_ref if check else record.get("final_ref"),
            "pin_scheduled": pin_scheduled,
            "keep_state": check.keep_state if check else None,
            "promoted": promoted,
        },
        status_code=201,
    )


@app.delete("/favorites")
async def favorite_remove(
    _: None = Depends(require_curator),
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
    request: Request,
    file: UploadFile,
    _: None = Depends(require_curator),
    expect_cid: str | None = Query(
        None,
        description="Canonical recovery: pin only if the bytes reproduce this CID (409 otherwise)",
    ),
):
    """Upload into Curio's static store. Uploads never enter IPFS implicitly."""
    if expect_cid:
        return JSONResponse(
            {"error": "expect_cid applies to explicit canonical IPFS recovery, not static uploads"},
            status_code=400,
        )
    settings = get_settings()
    store = StaticStore(settings.static_root)
    store.root.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        # UploadFile may be backed by a spooled file, but read() still returns
        # an unbounded byte string. Copy in chunks into our state filesystem so
        # the size limit is enforced without buffering the complete upload.
        with tempfile.NamedTemporaryFile(dir=store.root, prefix=".upload-", delete=False) as output:
            temporary = Path(output.name)
            size = 0
            while chunk := await file.read(65536):
                size += len(chunk)
                if size > settings.static_max_bytes:
                    return JSONResponse({"error": f"body exceeds {settings.static_max_bytes} bytes"}, status_code=413)
                output.write(chunk)
        entry = store.put_file(
            temporary, media_type=file.content_type, filename=file.filename,
            source_ref=f"upload:{file.filename}", keep_state="kept",
        )
        temporary = None  # put_file consumes or removes it in every path.
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        await file.close()
    return JSONResponse(
        {**entry, "filename": file.filename, "media_url": f"{request_origin(request)}/media/{entry['id']}",
         "source_kind": "upload", "integrity": {"algorithm": "sha256", "digest": entry["digest"]}},
        status_code=201,
    )


@app.post("/keep")
async def keep(request: Request, ref: str = Query(...), _: None = Depends(require_curator)):
    result = await resolve_ref(ref, get_settings(), app.state.client, origin=request_origin(request))
    if not result.resolved:
        return JSONResponse(result.as_dict(), status_code=422)
    if result.keep_state == "live-dependent":
        return JSONResponse({**result.as_dict(), "keep_state": "live-dependent",
            "error": "HTML capture has uncaptured runtime dependencies"}, status_code=409)
    if result.source_kind in {"http", "data", "upload"}:
        if not _promote_static(result):
            return JSONResponse({"error": "static media missing"}, status_code=404)
        result.keep_state = "kept"
        return JSONResponse(result.as_dict())
    try:
        outcome = await pin_resolved(result, get_settings(), app.state.client, why="keep")
    except Exception as exc:
        return JSONResponse({**result.as_dict(), "keep_state": "failed", "error": str(exc)}, status_code=502)
    if outcome not in {"pinned", "kept"}:
        return JSONResponse({**result.as_dict(), "keep_state": "failed",
            "error": "content was not successfully retained"}, status_code=502)
    result.keep_state = "kept"
    return JSONResponse(result.as_dict())


@app.get("/library")
async def library():
    return JSONResponse(await library_status(get_settings(), app.state.client))


@app.get("/media/{file_id}")
async def media(file_id: str):
    item = StaticStore(get_settings().static_root).get(file_id)
    if item is None:
        return JSONResponse({"error": "media not found"}, status_code=404)
    record, path = item
    # No filename= here: Starlette otherwise emits Content-Disposition:
    # attachment, which tells browsers/media renderers not to play inline.
    return FileResponse(path, media_type=record.get("media_type") or "application/octet-stream")


async def _gateway_proxy(request: Request, backend: str, path: str):
    """One public origin for native gateways; backend ports stay private."""
    settings = get_settings()
    base = (settings.ipfs_internal if backend == "ipfs" else
            settings.arweave_retained_internal if backend == "arweave-retained" else
            settings.arweave_internal)
    upstream_path = f"/ipfs/{path}" if backend == "ipfs" else f"/{path}"
    url = f"{base.rstrip('/')}{upstream_path}"
    if request.url.query:
        url += f"?{request.url.query}"
    headers = {}
    if request.headers.get("range"):
        headers["range"] = request.headers["range"]
    # Core can retrieve a cold transaction after the normal resolver HTTP
    # deadline. Native Arweave streams get the explicit cold-read budget;
    # IPFS keeps the general client timeout.
    timeout = settings.arweave_cold_timeout if backend.startswith("arweave") else None
    upstream = await app.state.client.send(
        app.state.client.build_request(request.method, url, headers=headers, timeout=timeout), stream=True,
    )
    headers = {k: v for k, v in upstream.headers.items() if k.lower() in
               {"content-type", "content-length", "content-range", "accept-ranges", "etag", "cache-control", "x-cache"}}
    return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code, headers=headers,
                             background=BackgroundTask(upstream.aclose))


@app.api_route("/ipfs/{path:path}", methods=["GET", "HEAD"])
async def ipfs_gateway(request: Request, path: str):
    return await _gateway_proxy(request, "ipfs", path)


@app.api_route("/arweave/{path:path}", methods=["GET", "HEAD"])
async def arweave_gateway(request: Request, path: str):
    parsed = arweave_parts(f"ar://{path}")
    if parsed is not None:
        txid, retained_path = parsed
        # A kept identity never quietly falls back to the ordinary Core.
        # If the retained Core is unavailable its response is surfaced as a
        # degraded native-plane failure rather than substituted bytes.
        if retained_state(txid, retained_path, get_settings()) == "kept":
            return await _gateway_proxy(request, "arweave-retained", path)
    return await _gateway_proxy(request, "arweave", path)


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

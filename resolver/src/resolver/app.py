from __future__ import annotations

from contextlib import asynccontextmanager
from importlib import metadata

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile

from . import operations
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
from .library import library_status
from .mcp_server import mcp, set_client
from .origin import effective_origin, normalize_origin
from .overrides import (
    DuplicateOverride,
    OverrideNotFound,
    OverrideRegistry,
    RegistryUnparseable,
    get_registry,
)
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
        settings = get_settings()
        effective = effective_origin(request, settings.public_base_url, settings.trusted_proxy_cidrs)
        if effective is None:
            return JSONResponse({"error": "invalid MCP Host"}, status_code=421)
        origin = request.headers.get("origin")
        if origin and normalize_origin(origin) != effective:
            return JSONResponse({"error": "invalid MCP Origin"}, status_code=403)
    return await call_next(request)


def request_origin(request: Request) -> str:
    """The configured, trusted-forwarded, or validated direct front door."""
    settings = get_settings()
    origin = effective_origin(request, settings.public_base_url, settings.trusted_proxy_cidrs)
    if origin is None:
        raise HTTPException(421, "invalid request Host")
    return origin


_SCOPE_DESCRIPTION = (
    "'held' = holdings; 'published' = works the wallet first-minted (Tezos only); "
    "'created' = works the wallet authored, i.e. creators/authors metadata, fully-burned "
    "dropped (Tezos only); 'contract' = every token of a token-contract address (both chains)"
)


@app.get("/resolve")
async def resolve_get(ref: str = Query(..., description="A reference previously stored by Curio")):
    """Redirect a known reference to its source-native Curio media route."""
    record = operations.lookup_resolution(ref, get_settings())
    if record is None:
        return JSONResponse({"error": "reference not found"}, status_code=404)
    if not record["playable"]:
        # Report the failure as absent rather than redirecting to a media
        # path the record itself says did not work.
        return JSONResponse(
            {"error": "reference is not playable", "reason": record.get("reason")},
            status_code=404,
        )
    return RedirectResponse(str(record["media_path"]), status_code=302)


@app.post(
    "/resolve",
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"ref": {"type": "string"}},
                        "required": ["ref"],
                    }
                },
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {"file": {"type": "string", "format": "binary"}},
                        "required": ["file"],
                    }
                },
            }
        }
    },
)
async def resolve_post(
    request: Request,
    ref: str | None = Query(None, description="A media reference to store"),
):
    """Resolve and store exactly one reference or uploaded file."""
    file: UploadFile | None = None
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("application/json"):
        try:
            body = await request.json()
        except ValueError:
            body = None
        body_ref = body.get("ref") if isinstance(body, dict) else None
        if isinstance(body_ref, str):
            if ref is not None:
                return JSONResponse({"error": "supply ref only once"}, status_code=400)
            ref = body_ref
    elif content_type.startswith("multipart/form-data"):
        candidate = (await request.form()).get("file")
        if isinstance(candidate, UploadFile):
            file = candidate

    if (ref is None) == (file is None):
        if file is not None:
            await file.close()
        return JSONResponse({"error": "supply exactly one of ref or file"}, status_code=400)

    settings = get_settings()
    origin = request_origin(request)
    if ref is not None:
        payload, stored = await operations.store_reference(
            ref, settings, app.state.client, origin
        )
        return JSONResponse(payload, status_code=200 if stored else 422)

    try:
        payload = await operations.store_upload(file, settings, origin)
    except operations.UploadTooLarge as exc:
        return JSONResponse({"error": str(exc)}, status_code=413)
    return JSONResponse(payload, status_code=201)


def _wallet_error(exc: Exception) -> JSONResponse:
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
    scope: str = Query("held", description=_SCOPE_DESCRIPTION),
    status: bool = Query(False, description="Also resolve each primary_ref and classify it (ok/substituted/unreachable/unresolvable/no-ref) — the audit view"),
    include_burned: bool = Query(False, description="created scope only: include fully-burned creations (default drops them — destroyed on purpose)"),
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
    return JSONResponse(result)


@app.post("/seed")
async def seed(
    ref: str = Query(..., description="Wallet address or name: 0x…, name.eth, tz1…, name.tez"),
    limit: int | None = Query(None, ge=1, description="Stop after this many tokens (for testing/incremental runs)"),
    scope: str = Query("held", description=_SCOPE_DESCRIPTION),
    include_burned: bool = Query(False, description="created scope only: include fully-burned creations (default drops them — destroyed on purpose)"),
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
    """Override input validated by overrides.validate_entry."""

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
    return JSONResponse(operations.override_listing(registry))


@app.post("/override")
async def override_add(request: Request, body: OverrideBody):
    registry = _registry()
    if registry is None:
        return _registry_disabled()
    try:
        payload = await operations.create_override(
            registry,
            body.model_dump(exclude={"replace"}),
            replace=body.replace,
            settings=get_settings(),
            client=app.state.client,
            origin=lambda: request_origin(request),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except (DuplicateOverride, RegistryUnparseable) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse(payload, status_code=201)


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
    return JSONResponse(operations.override_removed(removed))


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
    ref: str = Query(..., description="Any media reference (any spelling of it)"),
    note: str | None = Query(None, description="Optional short note"),
):
    favorites = _favorites_store()
    if favorites is None:
        return _favorites_disabled()
    try:
        created = await operations.create_favorite(
            favorites,
            ref,
            note,
            settings=get_settings(),
            client=app.state.client,
            origin=lambda: request_origin(request),
        )
    except (DuplicateFavorite, FavoritesUnparseable) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse(created.response(), status_code=201)


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
    return JSONResponse(operations.favorite_removed(removed))


@app.get("/library")
async def library():
    return JSONResponse(await library_status(get_settings(), app.state.client))


class DP1PlaylistBody(BaseModel):
    """DP-1 playlist request body: refs already stored by Curio."""

    refs: list[str] = Field(min_length=1)
    title: str | None = None
    duration: int | None = Field(default=None, gt=0)


@app.post("/playlist/dp1")
async def playlist_dp1(request: Request, body: DP1PlaylistBody):
    """Emit a complete, unsigned DP-1 1.0.0 playlist for catalogued refs."""
    try:
        playlist = operations.dp1_playlist(
            body.refs,
            get_settings(),
            request_origin(request),
            title=body.title,
            duration=body.duration,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    return JSONResponse(playlist)


# Media bytes are public web resources: browser players read them cross-origin
# (blob loaders, canvas, WebGL). Only the media routes carry this — the REST
# API is unauthenticated and stays same-origin.
_MEDIA_CORS = {"access-control-allow-origin": "*"}


@app.get("/media/{file_id}")
async def media(file_id: str):
    settings = get_settings()
    item = StaticStore(settings.static_root, settings.static_cache_max_bytes).get(
        _strip_display_extension(file_id)
    )
    if item is None:
        return JSONResponse({"error": "media not found"}, status_code=404)
    record, path = item
    # filename= would make Starlette send Content-Disposition: attachment.
    return FileResponse(
        path,
        media_type=record.get("media_type") or "application/octet-stream",
        headers=_MEDIA_CORS,
    )


def _strip_display_extension(path: str) -> str:
    """Strip a minted display extension from a serving path's first (identity)
    segment only. Identities (txids, CIDs, media ids) never contain a dot, so
    this only ever removes an extension Curio appended at minting time —
    a real manifest/inner path, which starts past the first "/", is untouched.
    """
    first, sep, rest = path.partition("/")
    name, dot, _ext = first.rpartition(".")
    return f"{name if dot else first}{sep}{rest}"


async def _gateway_proxy(request: Request, backend: str, path: str):
    """One public origin for native gateways; backend ports stay private."""
    settings = get_settings()
    path = _strip_display_extension(path)
    base = settings.ipfs_internal if backend == "ipfs" else settings.arweave_internal
    upstream_path = f"/ipfs/{path}" if backend == "ipfs" else f"/{path}"
    url = f"{base.rstrip('/')}{upstream_path}"
    if request.url.query:
        url += f"?{request.url.query}"
    # Request an uncompressed representation so raw proxy streaming cannot
    # mismatch a compressed body and its headers. Still forward encoding/vary
    # defensively for an upstream that ignores identity.
    headers = {"accept-encoding": "identity"}
    if request.headers.get("range"):
        headers["range"] = request.headers["range"]
    # Core can retrieve a cold transaction after the normal resolver HTTP
    # deadline. Native Arweave streams get the explicit cold-read budget;
    # IPFS keeps the general client timeout.
    timeout = settings.arweave_cold_timeout if backend.startswith("arweave") else None
    try:
        upstream = await app.state.client.send(
            app.state.client.build_request(request.method, url, headers=headers, timeout=timeout), stream=True,
        )
    except httpx.ConnectError:
        return JSONResponse({"error": "native backend unavailable"}, status_code=502)
    except httpx.HTTPError:
        return JSONResponse({"error": "native backend request failed"}, status_code=503)
    headers = {k: v for k, v in upstream.headers.items() if k.lower() in
               {"content-type", "content-length", "content-range", "accept-ranges", "etag", "cache-control", "x-cache", "content-encoding", "vary"}}
    headers.update(_MEDIA_CORS)
    return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code, headers=headers,
                             background=BackgroundTask(upstream.aclose))


@app.api_route("/ipfs/{path:path}", methods=["GET", "HEAD"])
async def ipfs_gateway(request: Request, path: str):
    return await _gateway_proxy(request, "ipfs", path)


@app.api_route("/arweave/{path:path}", methods=["GET", "HEAD"])
async def arweave_gateway(request: Request, path: str):
    return await _gateway_proxy(request, "arweave", path)


@app.get("/healthz")
async def healthz():
    try:
        version = metadata.version("content-resolver")
    except metadata.PackageNotFoundError:
        version = "unknown"
    result = await gateway_health(get_settings(), app.state.client)
    result["version"] = version
    return JSONResponse(result, status_code=200 if result["healthy"] else 503)


# Mount last so explicit REST and media routes take precedence.
app.mount("/", mcp.streamable_http_app())


def main() -> None:
    import logging

    import uvicorn

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    settings = get_settings()
    # Keep Uvicorn's proxy middleware off so Curio applies its CIDR allowlist.
    uvicorn.run(app, host=settings.host, port=settings.port, proxy_headers=False)

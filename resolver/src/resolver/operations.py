"""Shared workflows used by the REST and MCP adapters."""

from __future__ import annotations

import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

import httpx
from starlette.datastructures import UploadFile

from .config import Settings
from .favorites import Favorites
from .library import store_resolved
from .overrides import Override, OverrideRegistry, validate_entry
from .refs import canonical_ref_key
from .resolve import Resolved, resolve_ref, storage_intent
from .static_store import ResolutionStatus, StaticStore, playable


def store_static(result: Resolved, settings: Settings) -> bool:
    """Mark a source-native static object as stored without routing it through IPFS."""
    if result.source_kind not in {"http", "data", "upload"} or "/media/" not in result.resolved_url:
        return False
    return StaticStore(settings.static_root, settings.static_cache_max_bytes).store(
        result.resolved_url.rsplit("/", 1)[-1]
    )


def _media_path(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path
    if parsed.query:
        path = f"{path}?{parsed.query}"
    if not path.startswith(("/ipfs/", "/arweave/", "/media/")):
        raise ValueError("resolved media is not on a Curio media route")
    return path


def _digest_ref(prefix: str, result: Resolved) -> str | None:
    integrity = result.integrity or {}
    if integrity.get("algorithm") != "sha256" or not integrity.get("digest"):
        return None
    return f"{prefix}:sha256:{integrity['digest']}"


def _public_ref(ref: str, result: Resolved) -> str:
    if ref.startswith("data:"):
        return _digest_ref("data", result) or ref.strip()
    return ref.strip()


def resolution_payload(
    record: dict[str, object], origin: str, result: Resolved | None = None
) -> dict[str, Any]:
    """Format a stored resolution for API and MCP callers."""
    ref = str(record["ref"])
    payload: dict[str, Any] = {
        "ref": ref,
        "final_ref": record["final_ref"],
        "media_url": f"{origin.rstrip('/')}/resolve?{urlencode({'ref': ref})}",
        "status": record["status"],
    }
    if record.get("media_type") is not None:
        payload["media_type"] = record["media_type"]
    if result is not None:
        for name in (
            "source_kind",
            "playback_method",
            "title",
            "integrity",
            "substituted",
            "substituted_ref",
            "substitution_status",
        ):
            value = getattr(result, name)
            if value not in (None, False):
                payload[name] = value
    return payload


def failed_resolution_payload(ref: str, reason: str | None) -> dict[str, Any]:
    return {
        "ref": ref.strip(),
        "status": ResolutionStatus.FAILED.value,
        "reason": reason or "Curio could not resolve and store this reference.",
    }


def record_result(
    ref: str, result: Resolved, settings: Settings, origin: str
) -> dict[str, Any]:
    """Record the playback route after a final artifact has been stored."""
    public_ref = _public_ref(ref, result)
    final_ref = result.final_ref or public_ref
    if final_ref.startswith("data:"):
        final_ref = _digest_ref("data", result) or final_ref
    status = (
        ResolutionStatus.LIVE_DEPENDENT
        if result.status == ResolutionStatus.LIVE_DEPENDENT
        else ResolutionStatus.READY
    )
    store = StaticStore(settings.static_root, settings.static_cache_max_bytes)
    record = store.record_resolution(
        canonical_ref=canonical_ref_key(public_ref),
        ref=public_ref,
        final_ref=final_ref,
        media_path=_media_path(result.resolved_url),
        status=status,
        media_type=result.content_type,
        reason=result.note,
    )
    return resolution_payload(record, origin, result)


class UploadTooLarge(ValueError):
    """The uploaded body exceeded the configured static byte cap."""


async def store_upload(file: UploadFile, settings: Settings, origin: str) -> dict[str, Any]:
    """Persist a multipart upload as a static object and record its playback route."""
    store = StaticStore(settings.static_root, settings.static_cache_max_bytes)
    store.root.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=store.root, prefix=".upload-", delete=False) as output:
            temporary = Path(output.name)
            size = 0
            while chunk := await file.read(65536):
                size += len(chunk)
                if size > settings.static_max_bytes:
                    raise UploadTooLarge(f"body exceeds {settings.static_max_bytes} bytes")
                output.write(chunk)
        entry = store.put_file(
            temporary,
            media_type=file.content_type,
            filename=file.filename,
            source_ref=None,
            storage_status="stored",
        )
        temporary = None
        return record_upload(entry, filename=file.filename, settings=settings, origin=origin)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        await file.close()


def lookup_resolution(ref: str, settings: Settings) -> dict[str, Any] | None:
    """Canonicalize `ref` and return its stored resolution plus playability.

    None means Curio has no record of this reference at all — REST 404s,
    MCP's lookup tool answers found=false. `playable` mirrors
    static_store.playable(): False whenever the stored resolution failed.
    """
    record = StaticStore(settings.static_root, settings.static_cache_max_bytes).resolution(
        canonical_ref_key(ref)
    )
    if record is None:
        return None
    return {**record, "playable": playable(record)}


async def store_reference(
    ref: str,
    settings: Settings,
    client: httpx.AsyncClient,
    origin: str,
) -> tuple[dict[str, Any], bool]:
    """Resolve one reference, store its final artifact, and record its playback route."""
    with storage_intent():
        result = await resolve_ref(ref, settings, client, origin=origin)
    if not result.resolved:
        return failed_resolution_payload(ref, result.note), False

    try:
        if result.source_kind in {"http", "data", "upload"}:
            stored = store_static(result, settings)
        else:
            outcome = await store_resolved(result, settings, client)
            stored = outcome in {"pinned", "stored"}
    except Exception as exc:
        return failed_resolution_payload(ref, f"{type(exc).__name__}: {exc}"), False

    if not stored:
        return failed_resolution_payload(ref, "The final artifact could not be stored."), False

    return record_result(ref, result, settings, origin), True


def record_upload(
    entry: dict[str, object],
    *,
    filename: str | None,
    settings: Settings,
    origin: str,
) -> dict[str, Any]:
    """Give an uploaded static object the same reference contract as remote media."""
    ref = f"upload:sha256:{entry['digest']}"
    store = StaticStore(settings.static_root, settings.static_cache_max_bytes)
    record = store.record_resolution(
        canonical_ref=ref,
        ref=ref,
        final_ref=ref,
        media_path=f"/media/{entry['id']}",
        status=ResolutionStatus.READY,
        media_type=str(entry["media_type"]) if entry.get("media_type") else None,
    )
    payload = resolution_payload(record, origin)
    payload.update(
        {
            "filename": filename,
            "source_kind": "upload",
            "integrity": {"algorithm": "sha256", "digest": entry["digest"]},
        }
    )
    return payload


async def create_override(
    registry: OverrideRegistry,
    entry_data: dict[str, Any],
    *,
    replace: bool,
    settings: Settings,
    client: httpx.AsyncClient,
    origin: Callable[[], str],
) -> dict[str, Any]:
    """Validate and write an override, then disclose replacement availability."""
    entry = validate_entry(entry_data)
    replaced = registry.upsert(entry, replace=replace)
    try:
        # Replacement availability does not gate the write.
        replacement_result = await resolve_ref(entry.replacement, settings, client, origin=origin())
    except Exception:
        replacement_result = None
    return {
        "entry": asdict(entry),
        "canonical_key": canonical_ref_key(entry.ref),
        "replaced": replaced,
        "replacement_resolved": replacement_result.resolved if replacement_result else None,
        "replacement_resolved_url": (
            replacement_result.resolved_url
            if replacement_result and replacement_result.resolved
            else None
        ),
    }


def override_listing(registry: OverrideRegistry) -> dict[str, Any]:
    """The list_overrides shape shared by REST `GET /override` and MCP."""
    entries = [asdict(entry) for entry in registry.entries()]
    return {"count": len(entries), "entries": entries}


def override_removed(entry: Override) -> dict[str, Any]:
    """The remove-override shape shared by REST `DELETE /override` and MCP."""
    return {"removed": asdict(entry)}


def favorite_removed(record: dict[str, Any]) -> dict[str, Any]:
    """The remove-favorite shape shared by REST `DELETE /favorites` and MCP."""
    return {"removed": record}


@dataclass
class FavoriteCreation:
    """Shared favorite facts, formatted identically by both adapters."""

    record: dict[str, Any]
    result: Resolved | None

    def response(self) -> dict[str, Any]:
        result, record = self.result, self.record
        return {
            **record,
            "resolved": result.resolved if result else None,
            "resolved_url": result.resolved_url if result and result.resolved else None,
            "playback_method": result.playback_method if result else None,
            "final_ref": result.final_ref if result else record.get("final_ref"),
            "source_ref": result.final_ref if result else record.get("final_ref"),
        }


async def create_favorite(
    favorites: Favorites,
    ref: str,
    note: str | None,
    *,
    settings: Settings,
    client: httpx.AsyncClient,
    origin: Callable[[], str],
) -> FavoriteCreation:
    """Record a favorite and enrich it opportunistically."""
    try:
        # Resolution enriches the browse record but is never a gate.
        result = await resolve_ref(ref, settings, client, origin=origin())
    except Exception:
        result = None
    record = favorites.add(
        ref,
        title=result.title if result else None,
        note=note,
        final_ref=result.final_ref if result else None,
    )
    return FavoriteCreation(record, result)

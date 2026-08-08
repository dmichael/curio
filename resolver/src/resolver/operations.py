"""Shared mutation workflows used by the REST and MCP adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

import httpx

from .config import Settings
from .favorites import Favorites
from .library import pin_in_background, pin_resolved
from .overrides import OverrideRegistry, validate_entry
from .refs import canonical_ref_key
from .resolve import Resolved, resolve_ref
from .static_store import StaticStore


def promote_static(result: Resolved, settings: Settings) -> bool:
    """Promote a source-native static object without routing it through IPFS."""
    if result.source_kind not in {"http", "data", "upload"} or "/media/" not in result.resolved_url:
        return False
    return StaticStore(settings.static_root, settings.static_cache_max_bytes).keep(
        result.resolved_url.rsplit("/", 1)[-1]
    )


async def resolved_with_optional_pin(
    result: Resolved, settings: Settings, client: httpx.AsyncClient
) -> dict[str, Any]:
    """Return a resolved payload with explicit, source-appropriate keep intent."""
    payload = result.as_dict()
    if result.source_kind in {"http", "data", "upload"}:
        promoted = result.keep_state != "live-dependent" and promote_static(result, settings)
        payload["pin_scheduled"] = False
        payload["promoted"] = promoted
        if promoted:
            payload["keep_state"] = "kept"
        elif result.keep_state != "live-dependent":
            payload["keep_state"] = "failed"
    elif result.resolved and result.keep_state != "live-dependent" and result.source_kind == "ipfs":
        pin_in_background(result, settings, client, why="resolve pin")
        payload["pin_scheduled"] = True
        payload["keep_state"] = "pending"
    elif result.resolved and result.keep_state != "live-dependent" and result.source_kind == "arweave":
        payload["pin_scheduled"] = False
        payload["keep_state"] = (await pin_resolved(result, settings, client, why="resolve keep")) or "failed"
    else:
        payload["pin_scheduled"] = False
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


@dataclass
class FavoriteCreation:
    """Shared favorite mutation facts; adapters add their own response fields."""

    record: dict[str, Any]
    result: Resolved | None
    pin_scheduled: bool
    promoted: bool


async def create_favorite(
    favorites: Favorites,
    ref: str,
    note: str | None,
    *,
    settings: Settings,
    client: httpx.AsyncClient,
    origin: Callable[[], str],
    background_why: str = "pin",
) -> FavoriteCreation:
    """Record a favorite, enrich it opportunistically, and retain its media."""
    try:
        # A resolution failure must not prevent favoriting.
        result = await resolve_ref(ref, settings, client, origin=origin())
    except Exception:
        result = None
    record = favorites.add(
        ref,
        title=result.title if result else None,
        note=note,
        final_ref=result.final_ref if result else None,
    )
    pin_scheduled = bool(
        result
        and result.resolved
        and result.keep_state != "live-dependent"
        and result.source_kind not in {"http", "data", "upload", "arweave"}
    )
    promoted = False
    if result and result.resolved and result.source_kind in {"http", "data", "upload"}:
        promoted = result.keep_state != "live-dependent" and promote_static(result, settings)
        result.keep_state = "kept" if promoted else result.keep_state
    elif pin_scheduled:
        pin_in_background(result, settings, client, why=background_why)
        result.keep_state = "pending"
    elif result and result.resolved and result.keep_state != "live-dependent" and result.source_kind == "arweave":
        result.keep_state = (await pin_resolved(result, settings, client, why="favorite")) or "failed"
    return FavoriteCreation(record, result, pin_scheduled, promoted)

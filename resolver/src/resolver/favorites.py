"""Household favorites: owner-picked media references.

A favorite records "we like this work" and nothing else — the browse list a
consumer reads to pick something to play (pair each ref with /resolve).
Entries are keyed by canonical ref (refs.canonical_ref_key), so every
spelling of the same content — ipfs://CID, /ipfs/CID, gateway URLs,
ar://txid, arweave.net — is one favorite.

The store is a plain JSON list. Unlike the override registry's TOML this is
machine state, not a hand-authored document: the service's own API
(POST/DELETE /favorites, MCP add/remove_favorite) is the writer of record.
Hand edits still work — the file is reloaded whenever its mtime changes, no
restart needed. A missing file is an empty list (deploys set the path before
the first pick exists).

Same accepted race as overrides.py: an API write and an *external* write
landing in the same second can be invisible to the reader on
coarse-timestamp filesystems (the file itself is never corrupted) —
household scale, not worth locking.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .refs import canonical_ref_key
from .resolve import resolve_ref

_log = logging.getLogger("resolver.favorites")


class FavoriteError(Exception):
    """Base for favorites write errors; messages are operator-facing."""


class DuplicateFavorite(FavoriteError):
    """This content (under any spelling of its ref) is already a favorite."""


class FavoriteNotFound(FavoriteError):
    """No favorite (or no favorites file) matches."""


class FavoritesUnparseable(FavoriteError):
    """The on-disk file can't be parsed; refusing to rewrite over it."""


class Favorites:
    """Mtime-reloaded favorites list keyed by canonical ref."""

    _path: Path
    _mtime: float | None
    _table: dict[str, dict[str, Any]]

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._mtime = None
        self._table = {}

    def list_favorites(self) -> list[dict[str, Any]]:
        self._refresh()
        return [dict(record) for record in self._table.values()]

    # -- write path -----------------------------------------------------
    # Mutations are synchronous end to end (no await between read, modify,
    # and write), so in the single-event-loop process they cannot interleave
    # with another handler's mutation — no lock needed.

    def add(self, ref: str, title: str | None = None, note: str | None = None) -> dict[str, Any]:
        """Add `ref` as a favorite, rewriting the file; returns the record."""
        table = self._load_for_write()
        ref = ref.strip()
        key = canonical_ref_key(ref)
        if key in table:
            raise DuplicateFavorite(f"{key} is already a favorite")
        table[key] = {
            "ref": ref,
            "key": key,
            "title": title,
            "note": note,
            "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._write(table)
        return dict(table[key])

    def remove(self, ref: str) -> dict[str, Any]:
        """Remove the favorite matching any spelling of `ref`; returns it."""
        table = self._load_for_write()
        key = canonical_ref_key(ref)
        record = table.pop(key, None)
        if record is None:
            raise FavoriteNotFound(f"no favorite for {key}")
        self._write(table)
        return record

    def _load_for_write(self) -> dict[str, dict[str, Any]]:
        """Fresh, strict parse for read-modify-write.

        Reading a broken file keeps the previous table so one bad edit
        doesn't blank the list, but a rewrite would replace the file
        wholesale — never rewrite over a file you can't read.
        """
        try:
            text = self._path.read_text()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise FavoritesUnparseable(f"favorites file unreadable: {exc}") from exc
        try:
            return _parse(text)
        except ValueError as exc:
            raise FavoritesUnparseable(
                f"favorites file does not parse ({exc}); fix or remove it before writing"
            ) from exc

    def _write(self, table: dict[str, dict[str, Any]]) -> None:
        path = self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            "w", dir=path.parent, prefix=".favorites-", suffix=".tmp", delete=False
        )
        try:
            json.dump(list(table.values()), tmp, ensure_ascii=False, indent=2)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.chmod(tmp.name, 0o640)  # NamedTemporaryFile defaults to 0600
            os.replace(tmp.name, path)  # atomic: a crash can't leave a half-written list
        except BaseException:
            tmp.close()
            with contextlib.suppress(OSError):
                os.unlink(tmp.name)
            raise
        # Write-through: never depend on mtime to notice our own write —
        # same-second writes are invisible on coarse-timestamp filesystems.
        self._table = {key: dict(record) for key, record in table.items()}
        self._mtime = path.stat().st_mtime

    def _refresh(self) -> None:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            self._mtime, self._table = None, {}
            return
        if mtime == self._mtime:
            return
        # A broken edit keeps the previous table (a typo must not blank the
        # household's picks); mtime is still recorded so the parse error logs
        # once per edit, not per request.
        self._mtime = mtime
        try:
            table = _parse(self._path.read_text())
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            _log.warning("favorites %s not reloaded: %s", self._path, exc)
            return
        self._table = table
        _log.info("favorites %s loaded: %d entries", self._path, len(table))


def _parse(text: str) -> dict[str, dict[str, Any]]:
    """Strict parse shared by the read and write paths. Raises ValueError
    (json.JSONDecodeError included) with an operator-readable reason."""
    document = json.loads(text)
    if not isinstance(document, list):
        raise ValueError("favorites file is not a JSON list")
    table: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(document):
        if not isinstance(raw, dict):
            raise ValueError(f"entry {index} is not an object")
        ref = raw.get("ref")
        if not (isinstance(ref, str) and ref.strip()):
            raise ValueError(f"entry {index} is missing ref")
        ref = ref.strip()
        # The key is always recomputed so a hand edit to `ref` can't leave a
        # stale stored key behind.
        table[canonical_ref_key(ref)] = {
            "ref": ref,
            "key": canonical_ref_key(ref),
            "title": raw.get("title") if isinstance(raw.get("title"), str) else None,
            "note": raw.get("note") if isinstance(raw.get("note"), str) else None,
            "added_at": raw.get("added_at") if isinstance(raw.get("added_at"), str) else None,
        }
    return table


async def _resolved(
    record: dict[str, Any], settings: Settings, client: httpx.AsyncClient
) -> dict[str, Any]:
    try:
        result = await resolve_ref(record["ref"], settings, client)
    except Exception:
        return {**record, "resolved": False, "resolved_url": None, "playback_method": None}
    return {
        **record,
        "title": record["title"] or result.title,
        "resolved": result.resolved,
        "resolved_url": result.resolved_url if result.resolved else None,
        "playback_method": result.playback_method,
        "substituted": result.substituted,
    }


async def list_resolved(
    favorites: Favorites, settings: Settings, client: httpx.AsyncClient
) -> list[dict[str, Any]]:
    """Every favorite with its live resolution attached.

    Favorites are the browse surface, so the list answers "what do I hand
    the renderer" directly instead of requiring a /resolve round trip per
    pick. Resolutions run concurrently, and a failure degrades only its own
    entry (resolved: false) — one dead ref must not stall the list.
    """
    records = favorites.list_favorites()
    return list(await asyncio.gather(*(_resolved(r, settings, client) for r in records)))


@lru_cache
def get_favorites(path: str) -> Favorites:
    return Favorites(path)

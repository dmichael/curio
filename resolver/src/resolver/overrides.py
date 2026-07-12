"""Operator-curated exception registry: dead canonical ref -> replacement ref.

The registry is the machine-readable layer of the operator's recovery
manifest. It exists for the rare works whose canonical media is gone — an
unhashed URL on a dead domain, a CID with no providers and no faithful HTTP
copy — where the operator has chosen replacement bytes and stands behind
them. Everything ordinary resolves without it.

Substitutions are never silent: the resolver marks results `substituted`
and carries the entry's provenance `status`, so a consumer always knows
whether it got the verified original or a surrogate (docs/design.md).

The registry is a TOML file of `[[override]]` tables. The service's own
write API (POST/DELETE /override, MCP add/remove_override) is the primary
writer; hand edits still work — the file is reloaded whenever its mtime
changes, no restart needed. Machine writes regenerate the whole file, so
hand-written comments do not survive them. A missing file is an empty
registry (deploys may set the path before the first exception exists).

One accepted race: an API write and an *external* write landing in the same
second can be invisible to the reader on coarse-timestamp filesystems (the
file itself is never corrupted) — household scale, not worth locking.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .refs import canonical_ref_key

# Provenance tiers, strongest evidence first:
#   canonical-recovered — replacement bytes reproduce the recorded CID
#   captured-original   — fetched from the canonical URL while it answered;
#                         source and capture time recorded then
#   operator-attested   — no hash ever existed; the operator attests the
#                         local copy is the work (record source + checksum)
#   alternate-master    — different bytes (e.g. a platform HR master) that
#                         do NOT reproduce the canonical content
STATUSES = frozenset(
    {"canonical-recovered", "captured-original", "operator-attested", "alternate-master"}
)

_log = logging.getLogger("resolver.overrides")


class OverrideError(Exception):
    """Base for registry write errors; messages are operator-facing."""


class DuplicateOverride(OverrideError):
    """An entry for this canonical ref already exists (pass replace to update)."""


class OverrideNotFound(OverrideError):
    """No entry (or no registry file) matches."""


class RegistryUnparseable(OverrideError):
    """The on-disk file can't be parsed; refusing to rewrite over it."""


@dataclass(frozen=True)
class Override:
    ref: str  # the dead canonical reference, as written in the registry
    replacement: str  # what to resolve instead
    status: str  # provenance tier, one of STATUSES
    token: str | None = None  # e.g. CAIP-19; provenance metadata, not a key
    source: str | None = None
    captured: str | None = None
    note: str | None = None


class OverrideRegistry:
    """Mtime-reloaded lookup table keyed by canonical ref."""

    _path: Path
    _mtime: float | None
    _table: dict[str, Override]

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._mtime = None
        self._table = {}

    def lookup(self, ref: str) -> Override | None:
        self._refresh()
        return self._table.get(canonical_ref_key(ref))

    def entries(self) -> list[Override]:
        self._refresh()
        return list(self._table.values())

    def raw_text(self) -> str:
        """The registry file verbatim — the snapshot-back surface."""
        try:
            return self._path.read_text()
        except OSError as exc:
            raise OverrideNotFound(f"no overrides file at {self._path}") from exc

    # -- write path -----------------------------------------------------
    # Mutations are synchronous end to end (no await between read, modify,
    # and write), so in the single-event-loop process they cannot interleave
    # with another handler's mutation — no lock needed.

    def upsert(self, entry: Override, replace: bool = False) -> bool:
        """Add `entry`, rewriting the file. Returns True when it replaced an
        existing entry (requires replace=True; DuplicateOverride otherwise)."""
        table = self._load_for_write()
        key = canonical_ref_key(entry.ref)
        replaced = key in table
        if replaced and not replace:
            raise DuplicateOverride(
                f"an override for {key} already exists; pass replace to update it"
            )
        table[key] = entry  # dicts keep insertion order: replace edits in place
        self._write(table)
        return replaced

    def remove(self, ref: str) -> Override:
        """Remove the entry matching any spelling of `ref`; returns it."""
        table = self._load_for_write()
        key = canonical_ref_key(ref)
        entry = table.pop(key, None)
        if entry is None:
            raise OverrideNotFound(f"no override for {key}")
        self._write(table)
        return entry

    def _load_for_write(self) -> dict[str, Override]:
        """Fresh, strict parse for read-modify-write.

        Stricter than the read path on purpose: reading skips bad entries so
        one typo doesn't kill the exception layer, but a rewrite would *drop*
        them — an operator's broken-but-fixable edit must never be bulldozed.
        """
        try:
            text = self._path.read_text()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise RegistryUnparseable(f"overrides file unreadable: {exc}") from exc
        try:
            document = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise RegistryUnparseable(
                f"overrides file does not parse ({exc}); fix or remove it before writing"
            ) from exc
        entries: Any = document.get("override", [])
        if not isinstance(entries, list):
            raise RegistryUnparseable("'override' is not an array of tables")
        table: dict[str, Override] = {}
        for index, raw in enumerate(entries):
            try:
                entry = validate_entry(raw)
            except ValueError as exc:
                raise RegistryUnparseable(
                    f"entry {index} is invalid ({exc}); fix it before writing"
                ) from exc
            table[canonical_ref_key(entry.ref)] = entry
        return table

    def _write(self, table: dict[str, Override]) -> None:
        path = self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            "w", dir=path.parent, prefix=".overrides-", suffix=".tmp", delete=False
        )
        try:
            tmp.write(_serialize(list(table.values())))
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.chmod(tmp.name, 0o640)  # NamedTemporaryFile defaults to 0600
            os.replace(tmp.name, path)  # atomic: a crash can't leave a half-written registry
        except BaseException:
            tmp.close()
            with contextlib.suppress(OSError):
                os.unlink(tmp.name)
            raise
        # Write-through: never depend on mtime to notice our own write —
        # same-second writes are invisible on coarse-timestamp filesystems.
        self._table = dict(table)
        self._mtime = path.stat().st_mtime

    def _refresh(self) -> None:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            self._mtime, self._table = None, {}
            return
        if mtime == self._mtime:
            return
        # A broken edit keeps the previous table (a typo must not kill the
        # exception layer); mtime is still recorded so the parse error logs
        # once per edit, not per request.
        self._mtime = mtime
        try:
            document = tomllib.loads(self._path.read_text())
        except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as exc:
            _log.warning("overrides %s not reloaded: %s", self._path, exc)
            return
        self._table = _build_table(document, self._path)
        _log.info("overrides %s loaded: %d entries", self._path, len(self._table))


_HEADER = """\
# Operator exception registry — dead canonical refs -> replacements.
#
# MACHINE-MANAGED: the resolver rewrites this file on POST/DELETE /override
# (and the MCP add/remove_override tools); comments do not survive those
# writes. Hand edits are still honored (reloaded on mtime change). Snapshot
# with: GET /override?raw=1
#
# Fields: ref, replacement, status, and optional token/source/captured/note.
# status: canonical-recovered | captured-original | operator-attested |
#         alternate-master   (strongest evidence first)
"""

_FIELD_ORDER = ("ref", "replacement", "status", "token", "source", "captured", "note")


def _serialize(entries: list[Override]) -> str:
    """Regenerate the registry file. json.dumps escaping is valid TOML
    basic-string escaping, so flat string-only tables need no TOML-writer
    dependency (a round-trip test in test_overrides.py holds this to it)."""
    blocks = []
    for entry in entries:
        lines = ["[[override]]"]
        for name in _FIELD_ORDER:
            value = getattr(entry, name)
            if value is not None:
                lines.append(f"{name} = {json.dumps(value, ensure_ascii=False)}")
        blocks.append("\n".join(lines))
    return _HEADER + "\n" + "\n\n".join(blocks) + ("\n" if blocks else "")


def _build_table(document: dict[str, Any], path: Path) -> dict[str, Override]:
    table: dict[str, Override] = {}
    entries: Any = document.get("override", [])
    if not isinstance(entries, list):
        _log.warning("overrides %s: 'override' is not an array of tables", path)
        return table
    for index, raw in enumerate(entries):
        entry = _parse_entry(raw, index, path)
        if entry is not None:
            table[canonical_ref_key(entry.ref)] = entry
    return table


def validate_entry(raw: Any) -> Override:
    """The one rulebook for override entries, shared by file parsing and the
    write API. Raises ValueError with an operator-readable reason."""
    if not isinstance(raw, dict):
        raise ValueError("not a table")
    entry: dict[str, Any] = raw
    ref, replacement, status = entry.get("ref"), entry.get("replacement"), entry.get("status")
    if not (isinstance(ref, str) and ref.strip()):
        raise ValueError("missing ref")
    if not (isinstance(replacement, str) and replacement.strip()):
        raise ValueError("missing replacement")
    if status not in STATUSES:
        raise ValueError(f"status {status!r} not one of {sorted(STATUSES)}")
    if canonical_ref_key(ref) == canonical_ref_key(replacement):
        raise ValueError("replacement is the ref itself")
    optional = {
        key: value
        for key in ("token", "source", "captured", "note")
        if isinstance(value := raw.get(key), str)
    }
    return Override(ref=ref.strip(), replacement=replacement.strip(), status=status, **optional)


def _parse_entry(raw: Any, index: int, path: Path) -> Override | None:
    try:
        return validate_entry(raw)
    except ValueError as exc:
        _log.warning("overrides %s: entry %d skipped: %s", path, index, exc)
        return None


@lru_cache
def get_registry(path: str) -> OverrideRegistry:
    return OverrideRegistry(path)

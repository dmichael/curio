"""Transactional registry and native retained-plane hydration for Arweave.

AR.IO r81 has no per-transaction pin API.  Curio therefore keeps selected
transactions by reading them through a private, separately persistent r81 Core
whose only upstream is the ordinary local Envoy.  This is intentionally not
called an upstream AR.IO pin.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import Settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connection(settings: Settings) -> sqlite3.Connection:
    path = Path(settings.arweave_retention_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """CREATE TABLE IF NOT EXISTS retained_arweave (
            txid TEXT NOT NULL, path TEXT NOT NULL, state TEXT NOT NULL,
            requested_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT,
            PRIMARY KEY (txid, path)
        )"""
    )
    return db


def record_intent(txid: str, path: str, settings: Settings) -> None:
    """Atomically persist keep intent before any native hydration starts."""
    db = _connection(settings)
    try:
        now = _now()
        db.execute(
            """INSERT INTO retained_arweave (txid, path, state, requested_at, updated_at, error)
               VALUES (?, ?, 'pending', ?, ?, NULL)
               ON CONFLICT(txid, path) DO UPDATE SET state='pending', updated_at=excluded.updated_at, error=NULL""",
            (txid, path, now, now),
        )
        db.commit()
    finally:
        db.close()


def _set_state(txid: str, path: str, state: str, settings: Settings, error: str | None = None) -> None:
    db = _connection(settings)
    try:
        db.execute(
            "UPDATE retained_arweave SET state=?, updated_at=?, error=? WHERE txid=? AND path=?",
            (state, _now(), error, txid, path),
        )
        db.commit()
    finally:
        db.close()


def retained_records(settings: Settings) -> list[dict[str, str | None]]:
    db = _connection(settings)
    try:
        return [dict(row) for row in db.execute("SELECT * FROM retained_arweave ORDER BY requested_at")]
    finally:
        db.close()


def retained_state(txid: str, path: str, settings: Settings) -> str | None:
    db = _connection(settings)
    try:
        row = db.execute("SELECT state FROM retained_arweave WHERE txid=? AND path=?", (txid, path)).fetchone()
        # A kept txid can be served at a manifest subpath that was not the
        # original keep target. Keep identity at txid level without inventing
        # a second generic object store.
        if row is None:
            row = db.execute("SELECT state FROM retained_arweave WHERE txid=? AND state='kept' LIMIT 1", (txid,)).fetchone()
        return str(row["state"]) if row is not None else None
    finally:
        db.close()


async def _consume(client: httpx.AsyncClient, url: str, timeout: float) -> None:
    async with client.stream("GET", url, timeout=timeout) as response:
        response.raise_for_status()
        async for _ in response.aiter_bytes(65536):
            pass


def _url(txid: str, path: str, settings: Settings) -> str:
    return f"{settings.arweave_retained_internal.rstrip('/')}/{txid}{path}"


async def keep_arweave(txid: str, path: str, settings: Settings, client: httpx.AsyncClient) -> str:
    """Hydrate then prove a subsequent private-native read before marking kept.

    The second fully consumed response is deliberately not a cache-header
    heuristic: it proves the retained Core can serve the original txid/path.
    Failures remain durable registry evidence rather than false ``kept``.
    """
    record_intent(txid, path, settings)
    try:
        url = _url(txid, path, settings)
        await _consume(client, url, settings.seed_pin_timeout)
        await _consume(client, url, settings.seed_pin_timeout)
    except (httpx.HTTPError, ValueError) as exc:
        _set_state(txid, path, "failed", settings, f"{type(exc).__name__}: {exc}")
        return "failed"
    _set_state(txid, path, "kept", settings)
    return "kept"


async def retained_available(txid: str, path: str, settings: Settings, client: httpx.AsyncClient) -> bool:
    """Registry plus retained Core availability; never fall back to Envoy."""
    if retained_state(txid, path, settings) != "kept":
        return False
    try:
        response = await client.head(_url(txid, path, settings), timeout=10.0)
        return response.is_success
    except httpx.HTTPError:
        return False

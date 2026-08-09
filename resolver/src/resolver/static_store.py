"""Curio's bounded local HTTP/data media backend."""
from __future__ import annotations

import fcntl
import hashlib
import mimetypes
import sqlite3
import threading
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from uuid import uuid4


class ResolutionStatus(StrEnum):
    """The complete public state of a submitted Curio reference."""

    READY = "ready"
    LIVE_DEPENDENT = "live-dependent"
    FAILED = "failed"


class CacheQuotaError(ValueError):
    """The evictable public cache cannot admit another object."""


class StaticStore:
    """Content-addressed objects and source records.

    Stored records are durable. Other records share an evictable quota; digests
    with stored records are excluded from both the quota and eviction.
    """

    _SCHEMA_VERSION = 3
    _initialized_paths: set[Path] = set()
    _initialization_locks: dict[Path, threading.Lock] = {}
    _initialization_locks_guard = threading.Lock()

    def __init__(self, root: str, cache_max_bytes: int = 1_000_000_000):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.db_path = self.root / "library.sqlite3"
        self.cache_max_bytes = cache_max_bytes

    @classmethod
    def _initialization_lock(cls, db_path: Path) -> threading.Lock:
        with cls._initialization_locks_guard:
            return cls._initialization_locks.setdefault(db_path, threading.Lock())

    def _migrate(self, db: sqlite3.Connection) -> None:
        db.execute(
            """CREATE TABLE IF NOT EXISTS media (
                id TEXT PRIMARY KEY, digest TEXT NOT NULL, filename TEXT,
                media_type TEXT, bytes INTEGER NOT NULL, storage_status TEXT NOT NULL,
                source_ref TEXT NOT NULL, retrieved_at TEXT NOT NULL,
                accessed_at TEXT NOT NULL
            )"""
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(media)")}
        if "keep_state" in columns and "storage_status" not in columns:
            db.execute("ALTER TABLE media RENAME COLUMN keep_state TO storage_status")
            columns.remove("keep_state")
            columns.add("storage_status")
        db.execute("UPDATE media SET storage_status = 'stored' WHERE storage_status = 'kept'")
        # Existing catalogues predate access tracking and allowed NULL source
        # refs. Give legacy rows stable per-record identities before adding the
        # uniqueness invariant.
        if "accessed_at" not in columns:
            db.execute("ALTER TABLE media ADD COLUMN accessed_at TEXT")
            db.execute("UPDATE media SET accessed_at = retrieved_at WHERE accessed_at IS NULL")
        db.execute("UPDATE media SET source_ref = 'legacy:' || id WHERE source_ref IS NULL")
        # A prior version could create duplicate source/digest records. Keep a
        # durable/newest representative before the migration's unique index.
        db.execute("""DELETE FROM media WHERE id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY source_ref, digest
                    ORDER BY CASE storage_status WHEN 'stored' THEN 1 ELSE 0 END DESC,
                             retrieved_at DESC, id DESC
                ) AS position
                FROM media
            ) WHERE position > 1
        )""")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS media_source_digest ON media(source_ref, digest)")
        db.execute("CREATE INDEX IF NOT EXISTS media_digest ON media(digest)")
        db.execute("CREATE INDEX IF NOT EXISTS media_cache_lru ON media(storage_status, accessed_at)")
        db.execute(
            """CREATE TABLE IF NOT EXISTS resolutions (
                canonical_ref TEXT PRIMARY KEY,
                ref TEXT NOT NULL,
                final_ref TEXT NOT NULL,
                media_path TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ready', 'live-dependent', 'failed')),
                media_type TEXT,
                reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        db.execute("CREATE INDEX IF NOT EXISTS resolutions_status ON resolutions(status)")

    def _ensure_initialized(self) -> None:
        db_path = self.db_path.resolve()
        with self._initialization_lock(db_path):
            if db_path in self._initialized_paths:
                return
            lock_path = self.root / ".library.sqlite3.init.lock"
            with lock_path.open("a") as lock_file:
                # flock covers separate resolver processes on Linux.
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                try:
                    db = sqlite3.connect(self.db_path, timeout=30)
                    try:
                        db.row_factory = sqlite3.Row
                        db.execute("PRAGMA busy_timeout = 30000")
                        version = db.execute("PRAGMA user_version").fetchone()[0]
                        if version < self._SCHEMA_VERSION:
                            db.execute("PRAGMA journal_mode=WAL")
                            db.execute("BEGIN IMMEDIATE")
                            try:
                                self._migrate(db)
                                db.execute(f"PRAGMA user_version = {self._SCHEMA_VERSION}")
                                db.commit()
                            except Exception:
                                db.rollback()
                                raise
                    finally:
                        db.close()
                finally:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
            self._initialized_paths.add(db_path)

    def _connection(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects.mkdir(parents=True, exist_ok=True)
        self._ensure_initialized()
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 30000")
        return db

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def _source_identity(source_ref: str | None, digest: str) -> str:
        # SQLite is a catalogue, not a place to retain an attacker-controlled
        # multi-megabyte data URI. The resolver still returns its original
        # final_ref; this is only a stable storage identity.
        if source_ref and source_ref.startswith("data:"):
            return f"data:sha256:{digest}"
        return source_ref or f"anonymous:sha256:{digest}"

    def put(self, data: bytes, *, media_type: str | None, filename: str | None,
            source_ref: str | None, storage_status: str = "cached") -> dict[str, object]:
        temporary = self.root / f".upload-{uuid4().hex}"
        self.root.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(data)
        try:
            return self.put_file(temporary, media_type=media_type, filename=filename,
                                 source_ref=source_ref, storage_status=storage_status)
        finally:
            temporary.unlink(missing_ok=True)

    def _cache_bytes(self, db: sqlite3.Connection) -> int:
        row = db.execute("""SELECT COALESCE(SUM(bytes), 0) AS total FROM (
            SELECT digest, MAX(bytes) AS bytes
            FROM media GROUP BY digest
            HAVING SUM(CASE WHEN storage_status = 'stored' THEN 1 ELSE 0 END) = 0
        )""").fetchone()
        return int(row["total"])

    def _evict_for(self, db: sqlite3.Connection, needed: int) -> list[str]:
        if needed > self.cache_max_bytes:
            raise CacheQuotaError(
                f"static cache quota ({self.cache_max_bytes} bytes) cannot admit a {needed}-byte object"
            )
        removed: list[str] = []
        while self._cache_bytes(db) + needed > self.cache_max_bytes:
            # Object-level LRU; digests with stored records are never evicted.
            victim = db.execute("""SELECT digest FROM media GROUP BY digest
                HAVING SUM(CASE WHEN storage_status = 'stored' THEN 1 ELSE 0 END) = 0
                ORDER BY MAX(accessed_at) ASC, digest ASC LIMIT 1""").fetchone()
            if victim is None:
                raise CacheQuotaError("static cache quota has no evictable objects")
            digest = str(victim["digest"])
            db.execute("DELETE FROM media WHERE digest = ? AND storage_status != 'stored'", (digest,))
            if db.execute("SELECT 1 FROM media WHERE digest = ?", (digest,)).fetchone() is None:
                removed.append(digest)
        return removed

    def put_file(self, temporary: Path, *, media_type: str | None, filename: str | None,
                 source_ref: str | None, storage_status: str = "cached") -> dict[str, object]:
        hasher = hashlib.sha256()
        size = 0
        with temporary.open("rb") as source:
            for chunk in iter(lambda: source.read(65536), b""):
                hasher.update(chunk)
                size += len(chunk)
        digest = hasher.hexdigest()
        source_identity = self._source_identity(source_ref, digest)
        path = self.objects / digest
        db = self._connection()
        removed: list[str] = []
        moved = False
        try:
            # Serialize quota calculation, eviction, and insertion across workers.
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT id, bytes, media_type, storage_status FROM media WHERE source_ref = ? AND digest = ?",
                (source_identity, digest),
            ).fetchone()
            if existing is not None:
                db.commit()
                return {"id": existing["id"], "digest": digest, "bytes": existing["bytes"],
                        "media_type": existing["media_type"], "storage_status": existing["storage_status"]}
            is_new_object = db.execute("SELECT 1 FROM media WHERE digest = ?", (digest,)).fetchone() is None
            if storage_status != "stored" and is_new_object:
                removed = self._evict_for(db, size)
            if not path.exists():
                temporary.replace(path)
                moved = True
            file_id = uuid4().hex
            now = self._now()
            inserted = db.execute(
                """INSERT INTO media (id, digest, filename, media_type, bytes, storage_status, source_ref, retrieved_at, accessed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_ref, digest) DO NOTHING""",
                (file_id, digest, filename, media_type, size, storage_status, source_identity, now, now),
            )
            if inserted.rowcount == 0:
                winner = db.execute(
                    "SELECT id, bytes, media_type, storage_status FROM media WHERE source_ref = ? AND digest = ?",
                    (source_identity, digest),
                ).fetchone()
                db.commit()
                return {"id": winner["id"], "digest": digest, "bytes": winner["bytes"],
                        "media_type": winner["media_type"], "storage_status": winner["storage_status"]}
            db.commit()
        except Exception:
            db.rollback()
            if moved:
                # A rolled-back transaction cannot reference a just-moved object.
                path.unlink(missing_ok=True)
            raise
        finally:
            db.close()
            temporary.unlink(missing_ok=True)
        # Remove objects only after records commit and no record references them.
        for old_digest in removed:
            (self.objects / old_digest).unlink(missing_ok=True)
        return {"id": file_id, "digest": digest, "bytes": size,
                "media_type": media_type, "storage_status": storage_status}

    def get(self, file_id: str) -> tuple[dict[str, object], Path] | None:
        db = self._connection()
        try:
            row = db.execute("SELECT * FROM media WHERE id = ?", (file_id,)).fetchone()
            if row is None:
                return None
            path = self.objects / row["digest"]
            if not path.is_file():
                return None
            db.execute("UPDATE media SET accessed_at = ? WHERE id = ?", (self._now(), file_id))
            db.commit()
            record = dict(row)
        finally:
            db.close()
        return record, path

    def store(self, file_id: str) -> bool:
        db = self._connection()
        try:
            result = db.execute("UPDATE media SET storage_status = 'stored' WHERE id = ?", (file_id,))
            db.commit()
            return result.rowcount == 1
        finally:
            db.close()

    def record_resolution(
        self,
        *,
        canonical_ref: str,
        ref: str,
        final_ref: str,
        media_path: str,
        status: ResolutionStatus,
        media_type: str | None = None,
        reason: str | None = None,
    ) -> dict[str, object]:
        """Insert or update the one playback route for a submitted reference."""
        if not media_path.startswith(("/ipfs/", "/arweave/", "/media/")):
            raise ValueError("resolution media_path must use a Curio media route")
        now = self._now()
        db = self._connection()
        try:
            db.execute(
                """INSERT INTO resolutions
                   (canonical_ref, ref, final_ref, media_path, status, media_type,
                    reason, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(canonical_ref) DO UPDATE SET
                       ref = excluded.ref,
                       final_ref = excluded.final_ref,
                       media_path = excluded.media_path,
                       status = excluded.status,
                       media_type = excluded.media_type,
                       reason = excluded.reason,
                       updated_at = excluded.updated_at""",
                (
                    canonical_ref,
                    ref,
                    final_ref,
                    media_path,
                    status.value,
                    media_type,
                    reason,
                    now,
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM resolutions WHERE canonical_ref = ?", (canonical_ref,)
            ).fetchone()
            db.commit()
            return dict(row)
        finally:
            db.close()

    def resolution(self, canonical_ref: str) -> dict[str, object] | None:
        """Return a stored reference's playback route without resolving it again."""
        db = self._connection()
        try:
            row = db.execute(
                "SELECT * FROM resolutions WHERE canonical_ref = ?", (canonical_ref,)
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            db.close()

    def guessed_type(self, filename: str | None) -> str | None:
        return mimetypes.guess_type(filename or "")[0]

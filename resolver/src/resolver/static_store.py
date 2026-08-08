"""Curio's bounded local HTTP/data media backend."""
from __future__ import annotations

import hashlib
import mimetypes
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class CacheQuotaError(ValueError):
    """The evictable public cache cannot admit another object."""


class StaticStore:
    """Content-addressed objects plus source records.

    Kept records are durable library content. All other records share one
    evictable object quota; a digest with any kept record is deliberately not
    charged to that quota or removed by cache eviction.
    """

    def __init__(self, root: str, cache_max_bytes: int = 1_000_000_000):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.db_path = self.root / "library.sqlite3"
        self.cache_max_bytes = cache_max_bytes

    def _connection(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 30000")
        db.execute("PRAGMA journal_mode=WAL")
        # Serialize schema upgrades across multiple resolver processes.
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """CREATE TABLE IF NOT EXISTS media (
                id TEXT PRIMARY KEY, digest TEXT NOT NULL, filename TEXT,
                media_type TEXT, bytes INTEGER NOT NULL, keep_state TEXT NOT NULL,
                source_ref TEXT NOT NULL, retrieved_at TEXT NOT NULL,
                accessed_at TEXT NOT NULL
            )"""
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(media)")}
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
                    ORDER BY CASE keep_state WHEN 'kept' THEN 1 ELSE 0 END DESC,
                             retrieved_at DESC, id DESC
                ) AS position
                FROM media
            ) WHERE position > 1
        )""")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS media_source_digest ON media(source_ref, digest)")
        db.execute("CREATE INDEX IF NOT EXISTS media_digest ON media(digest)")
        db.execute("CREATE INDEX IF NOT EXISTS media_cache_lru ON media(keep_state, accessed_at)")
        db.commit()
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
            source_ref: str | None, keep_state: str = "cached") -> dict[str, object]:
        temporary = self.root / f".upload-{uuid4().hex}"
        self.root.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(data)
        try:
            return self.put_file(temporary, media_type=media_type, filename=filename,
                                 source_ref=source_ref, keep_state=keep_state)
        finally:
            temporary.unlink(missing_ok=True)

    def _cache_bytes(self, db: sqlite3.Connection) -> int:
        row = db.execute("""SELECT COALESCE(SUM(bytes), 0) AS total FROM (
            SELECT digest, MAX(bytes) AS bytes
            FROM media GROUP BY digest
            HAVING SUM(CASE WHEN keep_state = 'kept' THEN 1 ELSE 0 END) = 0
        )""").fetchone()
        return int(row["total"])

    def _evict_for(self, db: sqlite3.Connection, needed: int) -> list[str]:
        if needed > self.cache_max_bytes:
            raise CacheQuotaError(
                f"static cache quota ({self.cache_max_bytes} bytes) cannot admit a {needed}-byte object"
            )
        removed: list[str] = []
        while self._cache_bytes(db) + needed > self.cache_max_bytes:
            # Object-level LRU: a shared digest remains recent when any source
            # record is accessed. HAVING excludes every digest kept by any
            # record, so cache cleanup never unlinks durable content.
            victim = db.execute("""SELECT digest FROM media GROUP BY digest
                HAVING SUM(CASE WHEN keep_state = 'kept' THEN 1 ELSE 0 END) = 0
                ORDER BY MAX(accessed_at) ASC, digest ASC LIMIT 1""").fetchone()
            if victim is None:
                raise CacheQuotaError("static cache quota has no evictable objects")
            digest = str(victim["digest"])
            db.execute("DELETE FROM media WHERE digest = ? AND keep_state != 'kept'", (digest,))
            # The HAVING predicate above made this a no-kept object. Keep this
            # check defensive for future schema/state changes.
            if db.execute("SELECT 1 FROM media WHERE digest = ?", (digest,)).fetchone() is None:
                removed.append(digest)
        return removed

    def put_file(self, temporary: Path, *, media_type: str | None, filename: str | None,
                 source_ref: str | None, keep_state: str = "cached") -> dict[str, object]:
        """Atomically promote a bounded temporary file into the object store."""
        digest_hash = hashlib.sha256()
        size = 0
        with temporary.open("rb") as source:
            for chunk in iter(lambda: source.read(65536), b""):
                digest_hash.update(chunk)
                size += len(chunk)
        digest = digest_hash.hexdigest()
        source_identity = self._source_identity(source_ref, digest)
        path = self.objects / digest
        db = self._connection()
        removed: list[str] = []
        moved = False
        try:
            # Serializes quota calculation, eviction, and the unique insert
            # across resolver workers/processes. The unique key remains the
            # race-safe backstop for old or externally-created catalogues.
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT id, bytes, media_type, keep_state FROM media WHERE source_ref = ? AND digest = ?",
                (source_identity, digest),
            ).fetchone()
            if existing is not None:
                db.commit()
                return {"id": existing["id"], "digest": digest, "bytes": existing["bytes"],
                        "media_type": existing["media_type"], "keep_state": existing["keep_state"]}
            is_new_object = db.execute("SELECT 1 FROM media WHERE digest = ?", (digest,)).fetchone() is None
            if keep_state != "kept" and is_new_object:
                removed = self._evict_for(db, size)
            if not path.exists():
                temporary.replace(path)
                moved = True
            file_id = uuid4().hex
            now = self._now()
            inserted = db.execute(
                """INSERT INTO media (id, digest, filename, media_type, bytes, keep_state, source_ref, retrieved_at, accessed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_ref, digest) DO NOTHING""",
                (file_id, digest, filename, media_type, size, keep_state, source_identity, now, now),
            )
            if inserted.rowcount == 0:
                winner = db.execute(
                    "SELECT id, bytes, media_type, keep_state FROM media WHERE source_ref = ? AND digest = ?",
                    (source_identity, digest),
                ).fetchone()
                db.commit()
                return {"id": winner["id"], "digest": digest, "bytes": winner["bytes"],
                        "media_type": winner["media_type"], "keep_state": winner["keep_state"]}
            db.commit()
        except Exception:
            db.rollback()
            if moved:
                # No successful row can refer to a just-moved object after a
                # rolled-back immediate transaction.
                path.unlink(missing_ok=True)
            raise
        finally:
            db.close()
            temporary.unlink(missing_ok=True)
        # Files disappear only after records commit. A shared digest is never
        # in this list while any source record still references it.
        for old_digest in removed:
            (self.objects / old_digest).unlink(missing_ok=True)
        return {"id": file_id, "digest": digest, "bytes": size,
                "media_type": media_type, "keep_state": keep_state}

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

    def keep(self, file_id: str) -> bool:
        db = self._connection()
        try:
            result = db.execute("UPDATE media SET keep_state = 'kept' WHERE id = ?", (file_id,))
            db.commit()
            return result.rowcount == 1
        finally:
            db.close()

    def guessed_type(self, filename: str | None) -> str | None:
        return mimetypes.guess_type(filename or "")[0]

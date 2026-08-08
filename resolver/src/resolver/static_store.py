"""Curio's local HTTP media backend.

Ordinary HTTP, data URIs, and uploads live here.  This deliberately has no
Kubo or AR.IO dependency: publishing a static object to another protocol is a
separate curator operation.
"""
from __future__ import annotations

import hashlib
import mimetypes
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class StaticStore:
    def __init__(self, root: str):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.db_path = self.root / "library.sqlite3"

    def _connection(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            """CREATE TABLE IF NOT EXISTS media (
                id TEXT PRIMARY KEY, digest TEXT NOT NULL, filename TEXT,
                media_type TEXT, bytes INTEGER NOT NULL, keep_state TEXT NOT NULL,
                source_ref TEXT, retrieved_at TEXT NOT NULL
            )"""
        )
        return db

    def put(self, data: bytes, *, media_type: str | None, filename: str | None,
            source_ref: str | None, keep_state: str = "cached") -> dict[str, object]:
        digest = hashlib.sha256(data).hexdigest()
        file_id = uuid4().hex
        path = self.objects / digest
        db = self._connection()
        try:
            existing = db.execute(
                "SELECT id, keep_state FROM media WHERE source_ref IS ? AND digest = ?", (source_ref, digest)
            ).fetchone()
            if existing is not None:
                return {"id": existing["id"], "digest": digest, "bytes": len(data),
                        "media_type": media_type, "keep_state": existing["keep_state"]}
            if not path.exists():
                temporary = path.with_suffix(".tmp")
                temporary.write_bytes(data)
                temporary.replace(path)
            db.execute(
                "INSERT INTO media VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (file_id, digest, filename, media_type, len(data), keep_state, source_ref,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            db.commit()
        finally:
            db.close()
        return {"id": file_id, "digest": digest, "bytes": len(data),
                "media_type": media_type, "keep_state": keep_state}

    def get(self, file_id: str) -> tuple[dict[str, object], Path] | None:
        db = self._connection()
        try:
            row = db.execute("SELECT * FROM media WHERE id = ?", (file_id,)).fetchone()
        finally:
            db.close()
        if row is None:
            return None
        path = self.objects / row["digest"]
        if not path.is_file():
            return None
        return dict(row), path

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

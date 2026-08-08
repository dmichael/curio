"""Operator byte intake: POST /store puts an uploaded file into Kubo, pinned,
with provenance recorded — the supply side of the override registry.

Storing bytes is deliberately separate from serving them: a stored CID does
nothing until the operator points a dead ref at it (POST /override). The
ledger is the same captures.jsonl the seed capture path writes; `upload:`
sources can never collide with its URL-keyed dedupe.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import UploadFile

from .config import Settings
from .library import captures_file


class CidMismatch(Exception):
    """Uploaded bytes do not reproduce the expected CID."""


async def store_upload(
    upload: UploadFile,
    settings: Settings,
    client: httpx.AsyncClient,
    expect_cid: str | None = None,
) -> dict[str, Any]:
    """Stream an upload into Kubo (CIDv1, pinned — add pins by default) and
    append a provenance record. Mirrors seed._capture_url's mechanics.

    With `expect_cid` this becomes canonical recovery — seed._recover_cid's
    semantics for operator-supplied bytes: add unpinned with the CID version
    the expected CID uses, pin only when the hash round-trips (cryptographic
    proof the bytes are the canonical content), CidMismatch otherwise. A
    failed attempt's unpinned add is left to Kubo's GC.

    Raises ValueError when the body exceeds seed_recover_max_bytes (the one
    knob for "largest body buffered to disk on its way into Kubo"); httpx
    errors propagate for the route to map.
    """
    if expect_cid:
        add_params = {"pin": "false"}
        if not expect_cid.startswith("Qm"):
            add_params["cid-version"] = "1"
    else:
        add_params = {"cid-version": "1"}

    digest = hashlib.sha256()
    size = 0
    filename = upload.filename or "upload"
    with tempfile.TemporaryFile() as buffer:
        while chunk := await upload.read(65536):
            size += len(chunk)
            if size > settings.seed_recover_max_bytes:
                raise ValueError(f"body exceeds {settings.seed_recover_max_bytes} bytes")
            digest.update(chunk)
            buffer.write(chunk)
        buffer.seek(0)
        response = await client.post(
            f"{settings.ipfs_api}/api/v0/add",
            params=add_params,
            files={"file": (filename, buffer)},
            timeout=settings.seed_pin_timeout,
        )
    response.raise_for_status()
    cid = json.loads(response.text.strip().splitlines()[-1])["Hash"]

    if expect_cid:
        if cid != expect_cid:
            raise CidMismatch(f"bytes hash to {cid}, not {expect_cid} — not the canonical content")
        pin = await client.post(
            f"{settings.ipfs_api}/api/v0/pin/add",
            params={"arg": f"/ipfs/{cid}"},
            timeout=settings.seed_pin_timeout,
        )
        pin.raise_for_status()

    stored_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record = {
        "source": f"upload:{filename}",
        "cid": cid,
        "sha256": digest.hexdigest(),
        "bytes": size,
        "content_type": upload.content_type,
        "captured_at": stored_at,
        "wallet": None,
    }
    path = captures_file(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(record) + "\n")

    return {
        "cid": cid,
        "sha256": record["sha256"],
        "bytes": size,
        "content_type": upload.content_type,
        "filename": filename,
        "stored_at": stored_at,
        "resolved_url": f"{settings.ipfs_public_base}/ipfs/{cid}",
    }

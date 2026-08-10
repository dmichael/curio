"""Reference parsing shared by resolution, the override registry, and seeding.

Pure string work — no network, no settings.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

_IPFS_PATH_RE = re.compile(r"^/?ipfs/([^/]+)(/.*)?$")
_TXID_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
# CID shape for subdomain gateways: lowercase base32 CIDv1 (the usual form —
# subdomains must be case-insensitive) or base58 CIDv0, which some gateways
# also serve. Keeps ordinary hosts like www.ipfs.tech from matching.
_SUBDOMAIN_CID_RE = re.compile(r"^(?:Qm[1-9A-HJ-NP-Za-km-z]{44}|b[a-z2-7]{50,})$")


def ipfs_parts(ref: str) -> tuple[str, str] | None:
    """Return (cid, path) for any IPFS-shaped reference, else None.

    Covers ipfs://, /ipfs/ paths, path-style gateway URLs, and subdomain
    gateways (`https://<cid>.ipfs.<host>/path`).
    """
    parsed = urlparse(ref)
    if parsed.scheme == "ipfs" and parsed.netloc:
        return parsed.netloc, parsed.path
    match = _IPFS_PATH_RE.match(parsed.path)
    if match and parsed.scheme in {"http", "https", ""}:
        return match.group(1), match.group(2) or ""
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        # netloc, not .hostname — base58 CIDv0 is case-sensitive.
        host = parsed.netloc.rsplit("@", 1)[-1].split(":", 1)[0]
        labels = host.split(".")
        if (
            len(labels) >= 4  # <cid>.ipfs.<host-with-a-dot>
            and labels[1].lower() == "ipfs"
            and _SUBDOMAIN_CID_RE.match(labels[0])
        ):
            return labels[0], parsed.path
    return None


def arweave_parts(ref: str) -> tuple[str, str] | None:
    """Return (txid, path) for any Arweave-shaped reference, else None.

    The path matters: Arweave path manifests resolve `txid/sub/path` to a
    distinct resource, so dropping or normalizing it serves the wrong content.
    """
    parsed = urlparse(ref)
    if parsed.scheme == "ar":
        if parsed.netloc:
            return parsed.netloc, parsed.path
        txid, separator, rest = parsed.path.removeprefix("/").partition("/")
        return (txid, f"/{rest}" if separator else "") if txid else None
    if parsed.scheme in {"http", "https"} and parsed.hostname in {
        "arweave.net",
        "www.arweave.net",
    }:
        txid, separator, rest = parsed.path.removeprefix("/").partition("/")
        if _TXID_RE.fullmatch(txid):
            return txid, f"/{rest}" if separator else ""
    return None


def arweave_txid(ref: str) -> str | None:
    parts = arweave_parts(ref)
    return parts[0] if parts else None


def canonical_ref_key(ref: str) -> str:
    """One key per piece of content, however the ref is spelled.

    `ipfs://CID/x`, `/ipfs/CID/x` and any gateway URL for it collapse to the
    same key (queries dropped — `?filename=` variants are the same content);
    likewise `ar://txid/x` and `arweave.net/txid/x`. Anything else keys as
    its stripped self.
    """
    ref = ref.strip()
    ipfs = ipfs_parts(ref)
    if ipfs is not None:
        cid, path = ipfs
        return f"ipfs://{cid}{path.rstrip('/')}"
    arweave = arweave_parts(ref)
    if arweave is not None:
        txid, path = arweave
        # A bare trailing slash names the transaction itself, so `ar://txid/`
        # keys as `ar://txid`. Deeper paths keep theirs: a manifest resolves
        # `txid/sub/` and `txid/sub` to potentially different resources.
        return f"ar://{txid}{'' if path == '/' else path}"
    return ref

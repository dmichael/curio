"""Inventory the plain-HTTP (non-content-addressed) refs across wallets.

These are the server-dependent references — viewer pages, render proxies,
platform CDNs. Groups them by host and flags the HTML-viewer-shaped ones.

Usage: python3 scripts/list_http_refs.py <curio-base> <wallet> [wallet...]
"""

import json
import sys
import urllib.parse
import urllib.request
from collections import defaultdict


def refs_of(base: str, wallet: str) -> list[str]:
    url = f"{base}/wallet?ref={urllib.parse.quote(wallet)}"
    with urllib.request.urlopen(url, timeout=120) as fh:
        tokens = json.load(fh)["tokens"]
    out = []
    for token in tokens:
        for ref in token["refs"]:
            if ref.startswith(("http://", "https://")) and "/ipfs/" not in ref:
                out.append((ref, token.get("name")))
    return out


def main() -> None:
    base, wallets = sys.argv[1], sys.argv[2:]
    by_host: dict = defaultdict(list)
    for wallet in wallets:
        for ref, name in refs_of(base, wallet):
            host = urllib.parse.urlparse(ref).hostname
            by_host[host].append((ref, name))
    for host in sorted(by_host, key=lambda h: -len(by_host[h])):
        entries = by_host[host]
        print(f"\n{host} — {len(entries)} refs")
        seen = set()
        for ref, name in entries:
            if ref in seen:
                continue
            seen.add(ref)
            print(f"  {name!r:40.40} {ref[:100]}")


if __name__ == "__main__":
    main()

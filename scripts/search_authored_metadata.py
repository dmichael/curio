#!/usr/bin/env python3
"""Search authored works' full TzKT metadata for a term (name/desc/tags/etc).

Usage: python3 scripts/search_authored_metadata.py <authored.json> <search-term> <hits-out.json>
       (authored.json is the output of split_authored.py; matching is case-insensitive)
"""
import json
import sys
import urllib.request

if len(sys.argv) < 4:
    sys.exit(
        "usage: search_authored_metadata.py <authored.json> <search-term> <hits-out.json>"
    )
authored = json.load(open(sys.argv[1]))["authored"]
term = sys.argv[2].lower()


def metadata(contract, token_id):
    url = (f"https://api.tzkt.io/v1/tokens?contract={contract}"
           f"&tokenId={token_id}&select=metadata")
    with urllib.request.urlopen(url, timeout=20) as r:
        rows = json.load(r)
    return (rows[0] if rows else {}) or {}


hits = []
for i, t in enumerate(authored, 1):
    md = metadata(t["contract"], t["token_id"])
    blob = json.dumps(md).lower()
    if term in blob:
        hits.append((t, md))
    if i % 40 == 0:
        print(f"  {i}/{len(authored)}", file=sys.stderr)

print(f"\n{term!r} found in {len(hits)} works:", file=sys.stderr)
for t, md in hits:
    tags = md.get("tags")
    print(f"  {md.get('name')!r}  [{t['contract']} #{t['token_id']}]  "
          f"tags={tags}", file=sys.stderr)
json.dump([{"token": t, "metadata": md} for t, md in hits],
          open(sys.argv[3], "w"), indent=1)

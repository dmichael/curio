#!/usr/bin/env python3
"""Extract the primary-media CIDs of the living created works, for a pin check.

Usage: python3 scripts/created_cids.py <curio-base> <wallet> <cids-out.txt> <rows-out.json>
"""
import json
import re
import sys
import urllib.parse
import urllib.request

if len(sys.argv) < 5:
    sys.exit("usage: created_cids.py <curio-base> <wallet> <cids-out.txt> <rows-out.json>")
base, wallet = sys.argv[1].rstrip("/"), sys.argv[2]
url = f"{base}/wallet?ref={urllib.parse.quote(wallet)}&scope=created"
with urllib.request.urlopen(url, timeout=90) as r:
    tokens = json.load(r)["tokens"]

cid_re = re.compile(r"(Qm[1-9A-HJ-NP-Za-km-z]{44}|bafy[0-9a-z]+)")
rows = []
for t in tokens:
    ref = t.get("primary_ref") or ""
    m = cid_re.search(ref)
    rows.append((m.group(1) if m else "", t.get("name"), t.get("contract"), t.get("token_id")))

with open(sys.argv[3], "w") as fh:
    for cid, *_ in rows:
        if cid:
            fh.write(cid + "\n")
json.dump([{"cid": c, "name": n, "contract": k, "token_id": i} for c, n, k, i in rows],
          open(sys.argv[4], "w"), indent=1)
print(f"{len(rows)} living created works; {sum(1 for r in rows if r[0])} with an IPFS primary CID")

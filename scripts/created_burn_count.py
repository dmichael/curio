#!/usr/bin/env python3
"""True fully-burned count among an address's CREATED works (creators/authors),
with a correctly paginated burn-address balance query.

Usage: python3 scripts/created_burn_count.py <tz-wallet-address>
"""
import json
import sys
import urllib.parse
import urllib.request

if len(sys.argv) < 2:
    sys.exit("usage: created_burn_count.py <tz-wallet-address>")
W = sys.argv[1]
BURN = "tz1burnburnburnburnburnburnburjAYjjX"
TZKT = "https://api.tzkt.io/v1"


def get(path, **params):
    url = f"{TZKT}/{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def paginate(path, **params):
    out, off = [], 0
    while True:
        page = get(path, offset=off, limit=1000, **params)
        out += page
        if len(page) < 1000:
            return out
        off += 1000


# CREATED = tokens crediting W in creators or authors metadata
created = {}
for field in ("metadata.creators.[*]", "metadata.authors.[*]"):
    for r in paginate("tokens", **{field: W},
                      select="contract.address as c,tokenId as t,totalSupply as ts,metadata.name as name"):
        created[(r["c"], r["t"])] = r
print(f"created works: {len(created)}")

# Burn-held editions, paginated, across just the contracts in the created set
contracts = sorted({c for c, _ in created})
burn = {}
for r in paginate("tokens/balances", account=BURN, **{"balance.gt": "0"},
                  **{"token.contract.in": ",".join(contracts)},
                  select="token.contract.address as c,token.tokenId as t,balance as b"):
    burn[(r["c"], r["t"])] = int(r["b"])
print(f"burn-held rows across those contracts: {len(burn)}")

fully, partial = [], []
for key, r in created.items():
    ts = int(r.get("ts") or 0)
    bh = burn.get(key, 0)
    if ts and bh >= ts:
        fully.append((r["name"], key))
    elif bh > 0:
        partial.append((r["name"], key, bh, ts))

print(f"\nFULLY burned created works (exclude by default): {len(fully)}")
for name, key in sorted(fully):
    print(f"  {name!r:45s} {key[0]} #{key[1]}")
print(f"\nPARTIALLY burned (keep — live editions remain): {len(partial)}")
for name, key, bh, ts in sorted(partial):
    print(f"  {name!r:45s} {bh}/{ts} burned  {key[0]} #{key[1]}")

#!/usr/bin/env python3
"""Split a first-minted catalog into authored vs collected using TzKT creators.

first-minter (Curio's 'published' scope) includes fxhash editions the
wallet merely minted/collected. TzKT token metadata carries a creators/authors
list; if the wallet is in it, the work is authored, else collected.

Usage: python3 scripts/split_authored.py <published.json> <tz-wallet-address> <out.json>
"""
import json
import sys
import urllib.request

if len(sys.argv) < 4:
    sys.exit("usage: split_authored.py <published.json> <tz-wallet-address> <out.json>")
LISTING = sys.argv[1]
WALLET = sys.argv[2]
DOMAINS = "KT1GBZmSxmnKJXGMdMLbugPfLyUPmuLSMwKS"


def creators_for(contract, token_id):
    url = (f"https://api.tzkt.io/v1/tokens?contract={contract}"
           f"&tokenId={token_id}&select=metadata")
    with urllib.request.urlopen(url, timeout=20) as r:
        rows = json.load(r)
    if not rows:
        return []
    md = rows[0] or {}
    vals = []
    for key in ("creators", "authors"):
        v = md.get(key)
        if isinstance(v, list):
            vals += v
        elif isinstance(v, str):
            vals.append(v)
    return vals


def main():
    toks = [t for t in json.load(open(LISTING))["tokens"]
            if t.get("contract") != DOMAINS]
    authored, collected, unknown = [], [], []
    for i, t in enumerate(toks, 1):
        try:
            cr = creators_for(t["contract"], t["token_id"])
        except Exception as e:  # noqa: BLE001
            unknown.append((t, str(e)))
            continue
        (authored if WALLET in cr else collected).append(t)
        if i % 40 == 0:
            print(f"  {i}/{len(toks)}", file=sys.stderr)
    print(f"\nauthored (wallet in creators): {len(authored)}", file=sys.stderr)
    print(f"collected (first-minted, not creator): {len(collected)}", file=sys.stderr)
    print(f"unknown/no-metadata: {len(unknown)}", file=sys.stderr)
    print("\ncollected sample:", file=sys.stderr)
    for t in collected[:15]:
        print(f"  {t.get('name')}  [{t['contract']}]", file=sys.stderr)
    json.dump(
        {"authored": authored, "collected": collected,
         "unknown": [t for t, _ in unknown]},
        open(sys.argv[3], "w"), indent=1)


if __name__ == "__main__":
    main()

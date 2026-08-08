#!/usr/bin/env python3
"""Audit the resolution status of a wallet's published (first-minted) catalog.

Reads the /wallet?scope=published listing, resolves each token's primary_ref
against Curio in browse mode (no pin), and classifies each work as
alive / substituted / dead. Pure read: nothing is pinned.

Usage: python3 scripts/audit_published.py <curio-base> [listing.json] [out.json]
       (listing defaults to published.json, output to audit_result.json)
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

if len(sys.argv) < 2:
    sys.exit("usage: audit_published.py <curio-base> [listing.json] [out.json]")
BOX = sys.argv[1].rstrip("/")
LISTING = sys.argv[2] if len(sys.argv) > 2 else "published.json"
OUT = sys.argv[3] if len(sys.argv) > 3 else "audit_result.json"

# Tezos Domains contract — .tez registrations, not artworks
DOMAINS = "KT1GBZmSxmnKJXGMdMLbugPfLyUPmuLSMwKS"


def resolve(ref):
    url = f"{BOX}/resolve?ref=" + urllib.parse.quote(ref, safe="")
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        return {"resolved": False, "note": f"resolve call failed: {e}"}


def classify(tok):
    ref = tok.get("primary_ref")
    if not ref:
        return {"status": "no-media", **tok}
    res = resolve(ref)
    if res.get("substituted"):
        status = "substituted:" + str(res.get("substitution_status"))
    elif res.get("resolved"):
        status = "alive"
    else:
        status = "dead"
    return {
        "status": status,
        "name": tok.get("name"),
        "contract": tok.get("contract"),
        "token_id": tok.get("token_id"),
        "mime": tok.get("mime"),
        "primary_ref": ref,
        "note": res.get("note"),
    }


def main():
    d = json.load(open(LISTING))
    toks = d["tokens"]
    works = [t for t in toks if t.get("contract") != DOMAINS]
    domains = [t for t in toks if t.get("contract") == DOMAINS]
    print(f"{len(toks)} first-minted; {len(domains)} .tez domains; "
          f"auditing {len(works)} media works", file=sys.stderr)

    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for i, r in enumerate(ex.map(classify, works), 1):
            results.append(r)
            if i % 20 == 0:
                print(f"  {i}/{len(works)}  ({time.time()-t0:.0f}s)", file=sys.stderr)

    json.dump({"domains": len(domains), "results": results}, open(OUT, "w"), indent=1)
    from collections import Counter
    c = Counter(r["status"].split(":")[0] for r in results)
    print("\n=== STATUS ===", file=sys.stderr)
    for k, v in c.most_common():
        print(f"  {v:4d}  {k}", file=sys.stderr)
    print(f"done in {time.time()-t0:.0f}s -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()

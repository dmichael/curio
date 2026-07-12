"""Probe: can Blockscout token-transfers enumerate an address's ETH mints?

An ETH "published works" index doesn't exist keylessly; the proxy is token
transfers FROM the zero address TO the wallet (mint events). This pages the
whole transfer history and reports every such mint — evidence for (or
against) building scope=published on this shape.

Usage: python3 scripts/probe_eth_mints.py [address]
"""

import json
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://eth.blockscout.com/api/v2"
ZERO = "0x0000000000000000000000000000000000000000"
UA = "ff1-content-sidecar probe (household archive tooling)"  # default urllib UA gets 403'd


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: probe_eth_mints.py <0x-wallet-address>")
    address = sys.argv[1]
    params: dict = {"type": "ERC-721,ERC-1155", "filter": "to"}
    mints, pages = [], 0
    while True:
        url = f"{BASE}/addresses/{address}/token-transfers?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(request, timeout=30) as fh:
            data = json.load(fh)
        time.sleep(0.3)  # be polite to the keyless API
        pages += 1
        for item in data.get("items", []):
            sender = ((item.get("from") or {}).get("hash") or "").lower()
            if sender == ZERO:
                token = item.get("token") or {}
                total = item.get("total") or {}
                mints.append(
                    (
                        token.get("address_hash") or token.get("address"),
                        total.get("token_id"),
                        token.get("name"),
                    )
                )
        next_page = data.get("next_page_params")
        if not next_page or pages >= 40:
            break
        params = {**next_page, "type": "ERC-721,ERC-1155", "filter": "to"}

    seen = set()
    for contract, token_id, name in mints:
        key = (contract, token_id)
        if key in seen:
            continue
        seen.add(key)
        print(f"{contract} #{token_id} | {name}")
    print(f"pages: {pages}, mint transfers: {len(mints)}, unique tokens: {len(seen)}")


if __name__ == "__main__":
    main()

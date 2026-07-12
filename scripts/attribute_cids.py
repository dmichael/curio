"""Attribute CIDs to the wallet tokens whose metadata references them.

Usage: python scripts/attribute_cids.py <cids.txt> <wallet> [<wallet> ...]

Walks each wallet's holdings live (TzKT for tz…/*.tez, Blockscout for 0x…)
and reports, for every input CID, which token(s) reference it — so a failed
pin can be judged by what it actually is (artwork, domain-name card, spam).
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request

CID_RE = re.compile(r"(?:Qm[1-9A-HJ-NP-Za-km-z]{44}|baf[a-z0-9]{20,})")


def fetch_json(url: str):
    # Cloudflare-fronted APIs (Blockscout, objkt) 403 requests without a UA.
    request = urllib.request.Request(url, headers={"User-Agent": "content-sidecar-scripts/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def tezos_tokens(address: str):
    if not address.startswith("tz"):
        domains = fetch_json(
            "https://api.tzkt.io/v1/domains?" + urllib.parse.urlencode({"name": address})
        )
        address = domains[0]["address"]["address"]
    offset = 0
    while True:
        params = urllib.parse.urlencode({
            "account": address, "balance.gt": 0, "token.standard": "fa2",
            "offset": offset, "limit": 200,
        })
        page = fetch_json(f"https://api.tzkt.io/v1/tokens/balances?{params}")
        for balance in page:
            token = balance.get("token") or {}
            contract = token.get("contract") or {}
            yield {
                "chain": "tezos",
                "contract": contract.get("alias") or contract.get("address"),
                "token_id": token.get("tokenId"),
                "name": (token.get("metadata") or {}).get("name"),
                "metadata": token.get("metadata") or {},
            }
        if len(page) < 200:
            return
        offset += 200


def eth_tokens(address: str):
    base = "https://eth.blockscout.com/api/v2"
    if not address.startswith("0x"):
        data = fetch_json(f"https://bens.services.blockscout.com/api/v1/1/domains/{address}")
        address = data["resolved_address"]["hash"]
    params: dict = {"type": "ERC-721,ERC-1155"}
    while True:
        query = urllib.parse.urlencode(params)
        data = fetch_json(f"{base}/addresses/{address}/nft?{query}")
        for item in data.get("items", []):
            token = item.get("token") or {}
            yield {
                "chain": "ethereum",
                "contract": token.get("name") or token.get("address_hash"),
                "token_id": item.get("id"),
                "name": (item.get("metadata") or {}).get("name") or token.get("name"),
                "metadata": {**(item.get("metadata") or {}),
                             "_image_url": item.get("image_url"),
                             "_animation_url": item.get("animation_url")},
            }
        next_page = data.get("next_page_params")
        if not next_page:
            return
        params = {**next_page, "type": "ERC-721,ERC-1155"}


def main() -> None:
    wanted = set(open(sys.argv[1]).read().split())
    attribution: dict[str, list[str]] = {}
    for wallet in sys.argv[2:]:
        tokens = tezos_tokens(wallet) if (wallet.startswith("tz") or wallet.endswith(".tez")) else eth_tokens(wallet)
        for token in tokens:
            fields_by_cid: dict[str, list[str]] = {}
            for field, value in (token["metadata"] or {}).items():
                for cid in CID_RE.findall(json.dumps(value)):
                    fields_by_cid.setdefault(cid, []).append(str(field))
            for cid, fields in fields_by_cid.items():
                if cid not in wanted:
                    continue
                label = (
                    f'{token["chain"]} | {token["contract"]} #{token["token_id"]} | '
                    f'{token["name"]} | fields={",".join(sorted(set(fields)))}'
                )
                attribution.setdefault(cid, []).append(label)
    for cid in sorted(wanted):
        for label in attribution.get(cid, ["<not referenced by any current holding>"]):
            print(f"{cid} | {label}")


if __name__ == "__main__":
    main()

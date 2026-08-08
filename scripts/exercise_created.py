#!/usr/bin/env python3
"""End-to-end exercise of the scope=created enumerator against LIVE TzKT (not mocks).

Enumerates a wallet's created works twice (default and include_burned) and
prints the counts. When expected counts are supplied, exits nonzero on any
mismatch; without them it just reports what it found.

Run from resolver/ with its venv:
  .venv/bin/python ../scripts/exercise_created.py <tz-wallet-address> \
      [--expect-living N] [--expect-with-burned N] \
      [--expect-burned-token CONTRACT:TOKEN_ID]
"""
import argparse
import asyncio
import sys

import httpx

from resolver.config import Settings
from resolver.wallets import _tezos_created_items

SETTINGS = Settings(
    ipfs_internal="http://ipfs.internal",
    arweave_internal="http://ar.internal",
    ipfs_api="http://kubo.internal",
    blockscout_base="http://bs.internal/api/v2",
    bens_base="http://bens.internal/api/v1/1",
    tzkt_base="https://api.tzkt.io/v1",  # LIVE
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wallet", help="tz… wallet address whose created works to enumerate")
    parser.add_argument("--expect-living", type=int, default=None,
                        help="expected count of living created works (burned dropped)")
    parser.add_argument("--expect-with-burned", type=int, default=None,
                        help="expected count with include_burned=True")
    parser.add_argument("--expect-burned-token", default=None, metavar="CONTRACT:TOKEN_ID",
                        help="a known fully-burned token that must be absent from the living "
                             "set and present in the include_burned set")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    async with httpx.AsyncClient() as client:
        living = [i async for i in _tezos_created_items(args.wallet, SETTINGS, client)]
        allc = [i async for i in _tezos_created_items(args.wallet, SETTINGS, client,
                                                      include_burned=True)]
    print(f"created, living (burned dropped): {len(living)}")
    print(f"created, include_burned          : {len(allc)}")
    print(f"fully-burned dropped by default  : {len(allc) - len(living)}")

    ok = True
    if args.expect_living is not None:
        match = len(living) == args.expect_living
        print(f"living matches expected ({args.expect_living}): {match}")
        ok = ok and match
    if args.expect_with_burned is not None:
        match = len(allc) == args.expect_with_burned
        print(f"include_burned matches expected ({args.expect_with_burned}): {match}")
        ok = ok and match
    if args.expect_burned_token:
        contract, _, token_id = args.expect_burned_token.partition(":")
        burned_key = (contract, token_id)
        lk = {(i["contract"], i["tokenId"]) for i in living}
        ak = {(i["contract"], i["tokenId"]) for i in allc}
        print("known-burned absent from living  :", burned_key not in lk)
        print("known-burned present in include  :", burned_key in ak)
        ok = ok and burned_key not in lk and burned_key in ak
    return 0 if ok else 1


sys.exit(asyncio.run(main()))

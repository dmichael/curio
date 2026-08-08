"""Try to reproduce a dead metadata CID from indexer-cached JSON content.

The original file bytes are gone (no providers, no archive copy), but the
JSON *content* survives in Blockscout's cache. If some plausible serialization
of that content hashes to the recorded CIDv0, the bytes are proven canonical
(-> canonical-recovered); if none does, the honest status is alternate-master.

CIDv0 here = base58(0x12 0x20 || sha256(dag-pb PBNode)) for a single-block
UnixFS file, which is what `ipfs add` produces with default settings for
anything under 256 KiB — metadata JSON always qualifies.

Usage: python3 scripts/reproduce_metadata_cid.py <target-cid> <cached-content.json> [out-path]
       (cached-content.json holds the metadata object as the indexer cached it — keys
       often arrive alphabetized there; the original order is unknown, so plausible
       orders are permuted below. With out-path, a match's exact bytes are written.)
"""

import hashlib
import itertools
import json
import sys

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(data: bytes) -> str:
    num = int.from_bytes(data, "big")
    out = ""
    while num:
        num, rem = divmod(num, 58)
        out = B58_ALPHABET[rem] + out
    return "1" * (len(data) - len(data.lstrip(b"\x00"))) + out


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def cidv0(content: bytes) -> str:
    # UnixFS Data message: Type=File(2), Data=content, filesize=len
    unixfs = b"\x08\x02" + b"\x12" + varint(len(content)) + content + b"\x18" + varint(len(content))
    # PBNode with no links: Data field (1) only
    pbnode = b"\x0a" + varint(len(unixfs)) + unixfs
    return b58encode(b"\x12\x20" + hashlib.sha256(pbnode).digest())


def candidates(fields: dict):
    key_orders = set(
        itertools.permutations(fields)
        if len(fields) <= 5
        else [tuple(sorted(fields))]
    )
    # blockscout may normalize an empty attributes list to {}
    attr_variants = [{}, []] if fields.get("attributes") in ({}, []) else [fields.get("attributes")]
    separators = [(",", ":"), (", ", ": ")]
    for order, attrs, seps in itertools.product(key_orders, attr_variants, separators):
        doc = {k: (attrs if k == "attributes" else fields[k]) for k in order}
        text = json.dumps(doc, separators=seps, ensure_ascii=False)
        yield text
        yield text + "\n"
    for indent in (2, 4):
        for attrs in attr_variants:
            doc = {k: (attrs if k == "attributes" else fields[k]) for k in fields}
            text = json.dumps(doc, indent=indent, ensure_ascii=False)
            yield text
            yield text + "\n"


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit("usage: reproduce_metadata_cid.py <target-cid> <cached-content.json> [out-path]")
    target = sys.argv[1]
    fields = json.load(open(sys.argv[2]))
    out_path = sys.argv[3] if len(sys.argv) > 3 else None
    tried = 0
    for text in candidates(fields):
        tried += 1
        if cidv0(text.encode()) == target:
            print(f"MATCH after {tried} candidates ({len(text)} bytes, trailing-newline={text.endswith(chr(10))}):")
            print(text)
            if out_path:
                with open(out_path, "wb") as fh:
                    fh.write(text.encode())
                print(f"exact bytes written to {out_path}")
            return
    print(f"no match in {tried} candidates")


if __name__ == "__main__":
    main()

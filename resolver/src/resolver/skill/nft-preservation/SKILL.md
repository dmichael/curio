---
name: nft-preservation
description: Store, inspect, and repair NFT media with Curio.
---

# NFT media with Curio

Use `GET /skill` for API instructions and `GET /openapi.json` for exact request
schemas.

## Store a work

```bash
curl -X POST --get 'https://curio.example/resolve' \
  --data-urlencode 'ref=ar://transaction-id/path'
```

A successful response has status `ready` or `live-dependent`, a Curio
`media_url`, and the final source identity. Curio pins IPFS CID roots, fully
fetches Arweave through its persistent AR.IO Core, and stores HTTP or inline
media in its static store.

`live-dependent` means Curio stored the primary HTML artifact but the runtime
still relies on uncaptured resources. Do not call that runtime complete.

AR.IO local storage does not create a new Arweave-network replica.

## Play a stored work

Use `media_url` exactly as returned. It is a GET URL; Curio redirects it to the
stored `/ipfs/...`, `/arweave/...`, or `/media/...` path. An unknown reference
returns 404.

## Sweep a wallet or contract

```bash
curl -X POST --get 'https://curio.example/seed' \
  --data-urlencode 'ref=tz1...' \
  --data-urlencode 'scope=published'
```

Poll `GET /seed/<job-id>`. Use `held` for current holdings, `published` for
Tezos first-mint history, `created` for Tezos creator metadata, or `contract`
for a literal supported contract. Ethereum and Tezos mainnets are supported.

## Repair a dead reference

Preserve the original reference and any available hash, size, token, and source
information. Upload a replacement with multipart `POST /resolve`, then add an
override with `POST /override`.

Choose the status that matches the evidence:

- `canonical-recovered`: bytes reproduce the content-addressed original;
- `captured-original`: an unhashed source was captured while live;
- `operator-attested`: the operator vouches for an unhashed copy;
- `alternate-master`: intentionally different bytes.

Submit the dead reference with `POST /resolve`. Its response should report
`substituted: true` and the chosen status. An upload alone does not become a
replacement.

## Renderer notes

Curio may add a filename query hint for renderers such as Feral File FF1 that
infer media type from the URL suffix. `play` is static media; `send` is HTML.

Curio has no user authentication and belongs on a trusted network.

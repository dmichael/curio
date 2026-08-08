---
name: nft-preservation
description: Resolve, keep, and repair NFT media with Curio.
---

# NFT media with Curio

Use `GET /skill` for API instructions and `GET /openapi.json` for exact request
schemas.

## Check a work

Resolve its media reference:

```text
GET /resolve?ref=<reference>
```

A usable result has `resolved: true` and a Curio `media_url`. Record the final
source identity:

- `/ipfs/...` keeps the CID and path;
- `/arweave/...` keeps the transaction and manifest path;
- `/media/...` is a Curio static object with a SHA-256 digest.

`live-dependent` means an HTML response still relies on uncaptured resources.
Do not call that runtime preserved.

## Keep it

Use the authenticated synchronous endpoint when completion matters:

```bash
curl -X POST -H 'Authorization: Bearer YOUR_TOKEN' --get \
  'https://curio.example/keep' \
  --data-urlencode 'ref=ar://transaction-id/path'
```

IPFS keep pins the CID root. Arweave keep fully fetches the work and verifies a
hit from the same persistent AR.IO Core used for playback. HTTP and inline
media are marked kept in Curio's static store.

AR.IO Core has automatic content cleanup disabled, so ordinary Arweave resolve
and playback also populate persistent local state. The keep call mainly forces
and verifies the download. It does not create a new Arweave replica.

## Sweep a wallet or contract

Start an authenticated seed job:

```bash
curl -X POST -H 'Authorization: Bearer YOUR_TOKEN' --get \
  'https://curio.example/seed' \
  --data-urlencode 'ref=tz1...' \
  --data-urlencode 'scope=published'
```

Poll `GET /seed/<job-id>`. Use `held` for current holdings, `published` for
Tezos first-mint history, `created` for Tezos creator metadata, or `contract`
for a literal supported contract. Ethereum and Tezos mainnets are supported.

## Repair a dead reference

Preserve the original reference and any available hash, size, token, and source
information. Upload a replacement with authenticated `POST /store`, then add an
override with authenticated `POST /override`.

Choose the status that matches the evidence:

- `canonical-recovered`: bytes reproduce the content-addressed original;
- `captured-original`: an unhashed source was captured while live;
- `operator-attested`: the curator vouches for an unhashed copy;
- `alternate-master`: intentionally different bytes.

Check the new `/resolve` response. It should report `substituted: true` and the
chosen status. An upload alone does not become a replacement.

## Renderer notes

Use `media_url` exactly as returned. Curio may add a filename query hint for
renderers such as Feral File FF1 that infer media type from the URL suffix.
`play` is static media; `send` is HTML.

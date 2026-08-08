---
name: nft-preservation
description: Triage, retain, and disclose repairs for NFT media using Curio's source-appropriate IPFS, Arweave, and static-media planes. Use when media will not resolve or play, when auditing a wallet or contract catalog, or when deciding whether a work is actually kept.
---

# NFT preservation — collector playbook

Fetch Curio's live API instructions from `GET /skill`. This playbook explains
what to preserve and what Curio can honestly claim.

## Mental model

- **Resolution is not keep.** A resolution can populate Kubo, the persistent
  AR.IO Core, or Curio-static cache. `cached` is not durable preservation.
- **Keep is source-appropriate.** IPFS keep pins the canonical DAG in Kubo and
  seeds it. Arweave keep fully fetches through the same persistent Core and
  verifies a native hit. HTTP, `data:`, and uploads stay in Curio static
  storage; Curio does not add them to IPFS implicitly.
- **Arweave wording matters.** Same-Core cache verification is not an AR.IO
  pin API and does not create a new Arweave replica or move content between
  tiers.
- **Runtime HTML is different.** A live HTML response can play, but scripts,
  workers, APIs, fonts, and origin behavior may still be upstream-dependent.
  Curio marks it `live-dependent` and does not call it kept.
- **Replacement is disclosure.** An override is an explicit mapping and every
  resolution says `substituted: true` with its evidence label.

## Triage

1. Resolve the reference: `GET /resolve?ref=...`. A usable success has a
   Curio-origin `media_url`; `resolved: false` means Curio cannot currently
   serve a local artifact. Do not treat an upstream URL as success.
2. Record the final source kind and identity. `/ipfs/...` preserves CID/path;
   `/arweave/...` preserves transaction/manifest path; `/media/...` is a
   Curio-static SHA-256 object.
3. Check `keep_state`. `cached` can disappear. `kept` is the desired result;
   `pending` means an IPFS convenience pin is still running; `degraded` or
   `failed` needs operator attention. `live-dependent` is not complete runtime
   preservation.
4. Inspect `GET /library` and `/healthz`. A daemon running or a Kubo address
   advertised does not prove public reachability. AR.IO Core exposes no public
   reachability proof, so `unknown` is the honest result.

## Keep one work

Use the authenticated synchronous endpoint when a completion result matters:

```bash
curl -X POST -H 'Authorization: Bearer YOUR_TOKEN' --get \
  'https://curio.example/keep' \
  --data-urlencode 'ref=ar://transaction-id/path'
```

For IPFS, this pins the canonical DAG. For Arweave, Curio fully consumes the
same-Core response, verifies a second native cache hit, and then reports the
keep result. Resolve/play also populate this persistent cache; keep is eager
fetch/verification, not a network replication claim. For HTTP/data, first
resolve it so it has a `/media/<id>` artifact, then keep promotes that static
object. If any promotion fails, record that failure; do not call a cache warm a
pin.

`GET /resolve?ref=...&pin=1` also requires the bearer token. It is useful when
an asynchronous IPFS pin is acceptable, but `pin_scheduled: true` only means
scheduled. Use `/keep` or later library status for completion evidence.

## Sweep a collection

Use an authenticated `POST /seed` to retain final artifacts from wallet or
contract discovery:

```bash
curl -X POST -H 'Authorization: Bearer YOUR_TOKEN' --get \
  'https://curio.example/seed' \
  --data-urlencode 'ref=tz1...' \
  --data-urlencode 'scope=published'
```

Poll `GET /seed/<id>`. The job is in-memory, so save its outcome separately if
needed. It keeps IPFS through Kubo, fetches/verifies Arweave through the same
persistent Core, and keeps ordinary
media in Curio static storage. It does not upload ordinary media to IPFS.

Use `scope=held` for holdings; Tezos `scope=published` for first-minted works;
Tezos `scope=created` for creator/author metadata; and `scope=contract` for a
literal `0x...` or `KT1...` contract on either supported chain. First minting
is not authorship. Ethereum has no keyless creator index, so contract sweeps are
its practical publication-catalog path. Current discovery covers Ethereum and
Tezos mainnets only.

## The recovery ladder, repairs, and evidence

When canonical media is genuinely unavailable, first preserve the evidence:
token, field, source reference, byte hash/size when available, and why the
replacement was chosen. Then create an authenticated override with `POST
/override`, supplying JSON `ref`, `replacement`, and one status:

- `canonical-recovered` — bytes reproduce the canonical content identity;
- `captured-original` — an unhashed source was captured while it was live;
- `operator-attested` — the operator stands behind an unhashed copy;
- `alternate-master` — intentionally different bytes.

Verify the resulting `/resolve` response and retain the override snapshot from
`GET /override?raw=1`. Do not silently point a dead source at a different
backend or call that canonical preservation.

`POST /store` accepts an authenticated multipart file and keeps it in Curio
static storage. It returns `media_url`, not a Kubo CID. The current public API
does not expose an upload-to-IPFS or `expect_cid` canonical-recovery endpoint;
do not document or rely on one. An upload becomes a replacement only after an
explicit override.

## FF1 and runtime notes

For a Feral File FF1 or another suffix-sniffing renderer, use `media_url`
exactly as returned. Curio may attach a filename query hint to a bare IPFS CID;
it is functional. Use `play` for static media and `send` for HTML.

An IPFS runtime directory may have a canonical CID and still be live-dependent
as a work if it needs network APIs or origin behavior. An Art Blocks-like
on-chain script plus external libraries, and a server-hosted viewer page, need
an independently captured/replayable dependency package before anyone can call
them preserved. Curio currently reports that limitation instead of inventing a
keep claim.

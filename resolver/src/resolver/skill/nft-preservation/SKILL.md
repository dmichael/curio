---
name: nft-preservation
description: Triage and repair dead or dying NFT media (IPFS, Arweave, plain-HTTP) using Curio — the recovery ladder, provenance tiers, collection sweeps, and runtime-work preservation. Invoke when a work won't resolve or play, when auditing wallets or a published catalog for rot, or when deciding how to pin/repair/attest collection media.
---

# NFT preservation — the collector's playbook

Distilled from real recoveries. Curio's own API docs live at
`GET /skill` on the box (and ride along as MCP instructions); this skill is
the layer above: *how to think and what to do* when media is dying.

## Mental model

- **Two planes, different physics.** IPFS content exists only while someone
  pins it — rot is the default, pins are the library. Arweave bytes are
  permanent at the protocol layer; the local ar-io cache is disposable
  performance (evictable by design), and a warm-list timer keeps curated
  txids fresh. `GET /library` reports both honestly: pins are durable;
  `currently_cached < known_warmed` means evictions.
- **The intent ladder.** Browse/resolve never pins (looking must not grow
  the library) → `resolve?pin=1` / `wallet?pin=1` is per-call keep-this
  intent → favoriting pins *and* records the pick → `/seed` is the
  make-everything-durable hammer. Unfavoriting never unpins.
- **Resolution and preservation are separate acts.** Captures and stored
  bytes serve nothing until an operator points a dead ref at them via the
  override registry. Substitution is never silent (`substituted: true` +
  provenance tier in every resolve).
- **The blind spot: published ≠ held.** Wallet seeding follows *holdings*.
  Works you minted but sold are protected by nobody — historically the most
  rotted corner of a collection. Sweep them explicitly (`scope=published`
  on Tezos, `scope=contract` for named ETH contracts).

## Triage a dead (or suspect) ref

1. `GET /resolve?ref=…` on the box. `resolved: false`, or `resolved: true`
   with `note: "gateway probe failed"` on a bare CID, means the box can't
   fetch it. `substituted: true` means it's already repaired — stop.
2. Confirm deadness off-box: two public gateways with `--max-time 15`
   (`ipfs.io/ipfs/<cid>`, `dweb.link/ipfs/<cid>`). A 200 from any cache is
   not death — it's your recovery source; move fast, caches evict.
3. Check the Wayback Machine: `archive.org/wayback/available?url=ipfs.io/ipfs/<cid>`
   (fetch actual bytes with the `id_` URL variant to avoid rewriting).
4. Attribute it: which token, which field (primary artifact vs display
   variant), who minted it. A dead thumbnail is not a dead work.

## The recovery ladder (strongest claim first — never skip rungs)

1. **Canonical recovery — bytes reproduce the CID.** If ANY copy of the
   exact bytes exists (your original file, a gateway cache, Wayback),
   upload it: `POST /store?expect_cid=<dead CID>`. The box adds at the
   matching CID version and pins only on hash round-trip (409 otherwise).
   A match resurrects the original CID for the whole network — **no
   override needed at all**. Seed jobs already attempt this automatically
   from gateway caches for every failed pin (`recovered` in job counts).
2. **Byte reproduction from indexer caches** (for metadata JSON): indexers
   (Blockscout, TzKT) often cache the *content* of dead metadata with keys
   re-ordered. `scripts/reproduce_metadata_cid.py` regenerates candidate
   serializations (key orders × separators × trailing-newline) and computes
   CIDv0 the way `ipfs add` does — a match is cryptographic proof, feed it
   to `/store?expect_cid=`. Validate the CID math against the known
   `ipfs add "hello world"` → `Qmf412jQ…` vector before trusting a run.
3. **Promote a capture** (`captured-original`): seeding archives unhashed
   plain-HTTP media into Kubo *while URLs still answer*, with provenance in
   `captures.jsonl`. If the URL later dies, point the override at the
   captured CID. Strong tier because evidence was recorded at capture time.
4. **Operator attestation** (`operator-attested`): no hash ever existed;
   you stand behind a local copy. Record source, checksum, and story.
5. **Alternate master** (`alternate-master`): different bytes — an HR
   master, a platform re-render, another edition's file. Last resort;
   always disclosed.

Write the override with `POST /override` / `add_override` — `ref` matches
any spelling of the content; record `token` (CAIP-19), `source`, `note`.
Then verify by resolving the dead ref (`substituted: true`, playable URL)
and, for canonical recoveries, sha256-compare served bytes to the source.
Snapshot the registry back: `GET /override?raw=1`.

## Sweeps (run these periodically, and always before believing "it's fine")

- Holdings: `POST /seed?ref=<wallet>` per wallet. Idempotent; failures are
  the interesting output (`errors` holds the first 20; the service journal
  has all).
- Published catalog: `POST /seed?ref=<tz-wallet>&scope=published` (TzKT
  first-minter; note fxhash collects also list the collector as first
  minter — harmless, they get pinned too). ETH has no keyless creator
  index: sweep your publication contracts by name,
  `POST /seed?ref=<0x-contract>&scope=contract`.
- Read results per work: map failed CIDs back to tokens via
  `GET /wallet?…&scope=…` refs before reporting — distinguish dead
  primaries from dead display variants.
- `GET /library` after: pin count, repo size, warm-ledger health,
  registry counts. One call answers "what does the box hold."

## Runtime (generative/interactive) works — taxonomy decides the move

- **fxhash-style (runtime on IPFS):** artifact CID contains the whole
  runtime; the seed's pin already preserved it. The `?fxhash=` seed rides
  the URL and survives resolution. Nothing more to do.
- **Art Blocks-style (code + seed on chain):** script and token hash live
  on Ethereum; only the generator/render servers are fragile. Captures of
  generator pages are evidence, not preservation (they fetch parts at load
  time). True preservation = reconstruct a self-contained HTML: on-chain
  script + pinned-version dependency library + `window.tokenData = {hash,
  tokenId}`, then `/store` it. Render-verify against the archived PNG.
- **On-chain `data:` tokenURIs:** self-contained; the resolver already
  handles them. Nothing to do.
- **Server-hosted viewer pages (weakest):** the seed is usually in the URL
  payload (on-chain-safe) but the interpreter is someone's web app. While
  the domain answers: mirror the viewer *with page requisites*
  (`wget --page-requisites`-style), add the directory to Kubo, record the
  dir CID. On death: override the viewer URL →
  `ipfs://<dir-cid>/viewer.html?<original query>` (query strings survive
  resolution). Verify the mirror actually runs offline first — absolute
  URLs and API calls break naive mirrors.

## Field notes (earned the hard way)

- **Versum display variants rot first**; primaries usually outlive them.
  A failed-CID list dominated by one platform's variants is normal — but
  check every failure for a dead *primary*, that's the emergency.
- **Unverified ETH contracts hide their tokenURI from indexers.** Read it
  on-chain: `eth_call` with selector `0xc87b56dd` + zero-padded token id;
  decode offset/length/string from the result.
- **Metadata documents are a pin gap**: indexers hand metadata inline, so
  seeding pins the media but not the tokenURI document itself (it's only
  gateway-cached). Until the service gains chain access, pin important
  metadata manifests explicitly with `resolve?ref=<tokenURI>&pin=1`.
- **Duplicate captures with different URL encodings** (`%28` vs `(`) are
  the same bytes under different spellings — the sha256 in the ledger
  tells you.
- **Move on gateway caches immediately.** Every recovery in the field
  notes existed because ipfs.io still remembered bytes whose providers
  were gone. That window closes silently.

# Curio — design

**Target host:** a small always-on Linux machine on a trusted LAN. The reference
resource class is four ARM64 cores, 4 GB RAM, and roughly 100 GB of storage.

## What it is

Curio gives renderers one local endpoint for IPFS, Arweave, NFT metadata, and
ordinary media URLs. The resolver returns a playable URL; Kubo and AR.IO serve
the bytes. This keeps resolution logic out of clients and routes
content-addressed media through local pins and caches.

Curio's library is the Kubo pin set plus explicit Arweave warm records and
operator state. Wallet lookup helps choose content, but Curio does not maintain
a marketplace-style catalog. `/wallet` reads public indexers live, and `/seed`
adds content only when asked.

## The three planes

The appliance runs three public-facing services:

- **Kubo**: IPFS gateway on `:8080`; its RPC API on `:5001` is private to the
  Compose network.
- **AR.IO**: Arweave gateway and cache on `:3000`; observer work is disabled.
- **Resolver**: FastAPI service on `:8090` for resolution, preservation jobs,
  operator state, OpenAPI, skills, and MCP.

### Why resolution belongs on the box, not in clients

Client-side resolution logic normalizes references to *public* gateways, bypassing
the box's own pins and cache, and every client has to re-learn the same quirks.
Moving it onto Curio:

- points every fetch at the box's local gateways, so the pins and cache are used;
- gives one stable origin any consumer can call;
- lets device-control clients shrink to pure device control.

### API

- `GET /resolve?ref=<anything>` → JSON `{resolved_url, playback_method, title,
  provider, content_type}`. Pure resolution, no bytes; `resolved_url` is a
  box-local gateway URL the consumer fetches directly. When the override
  registry substituted a replacement, the response additionally carries
  `substituted: true`, `substituted_ref` (the dead canonical ref), and
  `substitution_status` (the provenance tier).
- `GET /c?ref=` → 302 to the resolved media (for dumb renderers); 422 when
  resolution failed.
- `GET /wallet?ref=` → live, normalized NFT inventory of a wallet (browse/pick).
  `scope=held|published|created|contract`: holdings; first-minted Tezos works;
  Tezos works attributed through creator metadata; or every token from one
  literal contract address. Contract scope is available on both supported
  chains.
- `POST /seed?ref=<wallet>` → background job: pin every IPFS ref the wallet's
  NFTs carry, warm the Arweave cache, recover vanished content from HTTP copies
  when the bytes round-trip to the same CID. Takes the same `scope` as
  `/wallet` — published works no longer held are what holdings-seeding misses.
- `GET/POST/DELETE /favorites` → the household's favorites: a tiny JSON file of
  operator state on the box, keyed by canonical ref so any spelling of the
  same content matches. Owner-curated state in the same spirit as the override
  registry — a handful of explicit picks, not the forbidden index.
- `GET /library` → cross-plane library status: IPFS pin/repo counts, warmed
  Arweave txids live-checked against the cache, operator-state counts.
- `GET /healthz` → gateway reachability + deployed version.
- Self-documentation: `GET /skill` (agent instructions, served by the service),
  `/openapi.json` + `/docs` (schema), `/mcp` (the same capabilities as MCP tools
  over streamable HTTP).

### Reference types

| Input | Resolves to |
|---|---|
| `ipfs://CID/path`, `/ipfs/CID`, `https://<any-gw>/ipfs/CID`, `https://<cid>.ipfs.<any-gw>/path` | box IPFS gateway URL (+ filename hint if bare) |
| `ar://txid[/path]`, `https://arweave.net/txid[/path]` | box Arweave gateway URL (path manifests: the path is part of the identity) |
| UnixFS directory CID | descend into the largest child and recurse |
| tokenURI (http/ipfs/arweave JSON) | animation/artifact field, else largest image by Content-Length probe → recurse |
| `data:application/json[;base64],…` (on-chain tokenURI) | decode inline metadata → recurse; other `data:` media passes through |
| Verse artwork page (`verse.works/artworks/...`) | scrape tokenUri / iframeUrl / og:image → recurse |
| direct media URL | passthrough |
| ENS / wallet / tx / contract+tokenId | chain lookup → tokenURI → recurse — **not built: needs an RPC/indexer path chosen for the service** (input syntax: CAIP-19, with `tezos/fa2` as a documented local extension — CASA registers no Tezos asset namespace) |

### Renderer fixups the resolver owns

Learned from driving a real FF1; harmless to other consumers:

- **Bare CID → iframe bug:** renderers that sniff the file extension from the URL
  fall back to iframe rendering for extension-less URLs. Probe the internal
  gateway's Content-Type and append `?filename=art.<ext>`.
- **Directory-wrapped media:** Kubo answers a directory HEAD with 200 and no
  Content-Type; list the directory and descend rather than serving the listing.
- **Largest image variant:** metadata field names (`image` vs `image_url`) do not
  reliably indicate the full-size asset — probe Content-Length and pick the biggest.
- **Playback method:** `send` for live HTML works (load as a page), `play` for
  static media. Inferred from probed content type, falling back to URL shape.
- **mDNS:** some renderers (the FF1 included) cannot resolve `.local` names —
  resolved URLs always use the host's LAN IP.

## Dead works: the override registry and seed capture

The purist path — resolve the token's recorded reference, serve bytes that
verify against it — fails for works whose canonical media is genuinely gone:
CIDs with no providers and no faithful HTTP copy, and above all works minted
against ordinary URLs (no content hash) on domains that later died. Two
mechanisms cover this, deliberately kept apart:

**The override registry** (`overrides.py`, a TOML file, mtime-reloaded) maps a
dead canonical *ref* to a replacement ref. It is keyed by reference, not by
token, because dead refs are usually discovered mid-recursion — the metadata
still resolves, its `animation_url` is dead — and because every entry point
(token, tokenURI, raw CID, gateway URL) funnels through the same recursive
resolver. Any spelling of the same content matches. Entries carry a provenance
tier: `canonical-recovered` (bytes reproduce the CID), `captured-original`
(fetched from the canonical URL while it answered, provenance recorded then),
`operator-attested` (no hash ever existed; the operator stands behind a local
copy), `alternate-master` (different bytes, e.g. a platform HR master).
Substitution is never silent: responses carry `substituted`,
`substituted_ref`, and `substitution_status`, so a renderer just plays the
work while an archival consumer sees exactly what it got.

This is not the index the posture forbids: an override is non-derivable owner
knowledge — "the bytes at this dead URL are these bytes" exists nowhere else.
The registry holds only exceptions, stays human-readable, and is expected to
number in the dozens.

The registry is managed live over the API: full CRUD on `/override` (REST)
and `list/add/remove_override` (MCP), plus `POST /store` to put replacement
bytes into Kubo — pinned, CIDv1, provenance appended to the capture ledger —
before an override references them. The on-box TOML file is the source of
truth. Hand edits still work through mtime reload, but API writes regenerate
the file, so hand-written comments do not survive them. Snapshot it with
`GET /override?raw=1`.

**Seed capture** (`seed_capture_dir`) is the insurance that makes the worst
tier avoidable in the future: `/seed` archives unhashed plain-HTTP media into
Kubo *while the URL still answers*, recording source URL, capture time, size,
sha256, and the new CID in `captures.jsonl`. Captured copies are never served
automatically — promoting one into the registry (as `captured-original`) is an
operator decision. Resolution and preservation stay separate actions.

## Trust model

Curio 0.1 assumes a trusted LAN and has no authentication or TLS. It limits
accidental amplification but is not an isolation boundary:

- **Seeding is admission-controlled:** duplicate wallet jobs coalesce, at most
  `seed_max_active` jobs run at once, each job has a wall-clock cap, and job
  history is bounded.
- **Fetches are bounded:** any body the resolver buffers (metadata, scraped
  pages, directory listings) is capped by `fetch_max_bytes`; recovery and
  capture downloads are capped by `seed_recover_max_bytes`.
- **Internal targets are refused** in user- and metadata-supplied URLs: literal
  private/loopback/link-local IPs and `localhost` are rejected before any fetch
  or probe. The box's own gateways are exempt (fetching them is the point).

DNS names resolving to private addresses and redirect chains are not
revalidated. Anyone who can reach the resolver can seed content, upload bytes,
and rewrite operator state. `/c` can redirect to external URLs. These are
constraints of the trusted-LAN design, not protections against hostile input.
See [the security policy](../SECURITY.md) before deployment.

## Optional deep-archive peer

Curio is fully self-contained: public networks are its only required
upstream. A site that also runs a larger curated archive node can make it the
Curio's fast path — protect the connection with Kubo's `Peering.Peers` (the
connection manager prunes unprotected peers under pin load, and server-profile
nodes neither announce their LAN addresses nor answer mDNS), and point AR.IO's
trusted gateway at it. That is deployment-site configuration and lives outside
this repo; nothing here knows about any particular archive host.

## Open decisions

- **Serve vs redirect:** consumers fetch the raw gateway URL directly (resolver
  in the metadata path only); `/c` exists for renderers that need a redirect.
  Keeping Python out of the byte path is the default.
- **AR.IO weight:** a full ar-io-node is heavy for a pure cache; a lightweight
  Arweave proxy+cache inside the resolver may replace it if memory pressure bites.
  The cache is also evictable and has no inventory API, so the box keeps its own
  ledger of deliberately-warmed txids (`warmed.jsonl`, beside the capture ledger)
  and `/library` live-checks each entry via the gateway's `X-Cache` header —
  warmed is honestly weaker than pinned, and eviction is visible, not silent.
- **Not built: chain lookups.** ENS/wallet/tx/contract resolution in the
  resolve path needs an RPC/indexer path chosen for the *service* (the seeding
  surface already uses keyless public indexers: Blockscout/BENS and TzKT).

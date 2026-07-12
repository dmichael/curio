---
name: content-sidecar
description: Resolve any media reference (IPFS, Arweave, NFT tokenURI, verse.works page, direct URL) into a LAN-playable URL, and seed the box's content cache from a wallet. Fetched live from the sidecar itself at GET /skill/SKILL.md — the box is the source of truth for how to use it.
---

# Content sidecar — agent instructions

**No registration, no stored state.** Resolve works on arbitrary refs with zero
setup — unpinned content is fetched on demand and lands in the gateway cache.
Seeding is a one-shot imperative ("make this wallet's holdings durable now"),
not a subscription; the only state it leaves is pins. Cache = incidental,
pins = the library.

These instructions are served by the sidecar they describe (`GET /skill/SKILL.md`),
so they always match the running service. The machine-readable API schema is at
`GET /openapi.json` (interactive: `/docs`). Prefer this file for *how to use*
the service; prefer the schema for exact parameter shapes.

**Shipped skills:** the box also serves the collector's preservation playbook
— triage, the recovery ladder, provenance tiers, sweeps, runtime works — at
`GET /skill/nft-preservation`. Fetch it before repairing or auditing works;
an unknown `/skill/<name>` 404s with the list of what's available.

**MCP:** the same capabilities are exposed as MCP tools (streamable HTTP) at
`/mcp` — connect the sidecar as an MCP server (`"url": "http://<sidecar-ip>:8090/mcp"`)
and the tools `resolve`, `wallet_tokens`, `seed_wallet`, `seed_status`,
`health`, `library_status`, `list_overrides`, `add_override`,
`remove_override`, `list_favorites`, `add_favorite`, and `remove_favorite`
appear with schemas; this file rides along as the server instructions.

## Browse a wallet (pick something to play)

- `GET /wallet?ref=<wallet>` (`0x…`, `name.eth`, `tz1…`, `name.tez`; optional
  `limit=<n>`) — live, normalized NFT inventory straight from the public
  indexers. No snapshot files: this replaces reading `*-nfts.json` exports.
- Each token: `name`, `contract`, `token_id`, `mime`, `primary_ref` (the
  artwork's main media reference), `refs` (all media candidates).
- To display one: take its `primary_ref` → `GET /resolve?ref=…` → cast.
- Add `&pin=1` to also make everything listed durable: it starts a seed job
  for the wallet (honoring `limit` and `scope`), returned as `pin_job` — poll
  it at `GET /seed/{id}`.
- `&scope=published` (Tezos only) lists the works the wallet *first-minted* —
  instead of what it currently holds. A leaky proxy for authorship: it counts
  fxhash editions the wallet collected (it's their first minter) and misses
  editions a collector minted of the wallet's own work.
- `&scope=created` (Tezos only) is the robust authorship index: works crediting
  the wallet in `creators`/`authors` metadata — what it actually *made*.
  Fully-burned creations (every edition at a burn address) are dropped by
  default — destroyed on purpose, they are the lowest preservation priority;
  `&include_burned=1` keeps them. ETH has no keyless creator index at all (mint
  events name the minter, not the author), so `created` is Tezos-only.
- `&scope=contract` lists every token of one token contract, both chains
  (`ref` must be the literal `0x…`/`KT1…` contract address, not a name) — how
  an ETH publication is swept, since ETH has no keyless creator index.
- `&status=1` is the **audit view**: each token's `primary_ref` is resolved
  and classified in place — `ok` / `substituted` (already repaired) /
  `unreachable` (dead content) / `unresolvable` / `no-ref` — plus a
  `status_counts` summary. One call replaces a per-token resolve loop; when
  dead refs exist, expect the call to take about one probe timeout.

## Play anything on a renderer (e.g. a Feral File FF1)

1. `GET /resolve?ref=<anything>` — the ref may be `ipfs://…`, `/ipfs/…`, any
   gateway URL, `ar://<txid>[/path]`, an `arweave.net` URL, a tokenURI (JSON
   metadata, including on-chain `data:` URIs), a `verse.works/artworks/…`
   page, or a direct media URL.
2. Read the response: `resolved_url` (LAN-fetchable), `playback_method`,
   `title`, `content_type`.
3. Hand `resolved_url` to the renderer **exactly as returned** — query params
   like `?filename=art.png` are functional (they fix extension-sniffing
   renderers), not cosmetic.
4. `playback_method` semantics: `play` = static media (image/video);
   `send` = load as a web page (live/generative HTML works).
5. `resolved: false` means the ref was recognized but couldn't be resolved;
   `note` says why. Don't cast unresolved URLs.
5b. Add `&pin=1` to also pin the resolved content onto the box (IPFS) or
   warm its cache (Arweave), in the background — `pin_scheduled` reports it.
   Plain resolution never pins; pass `pin` only for keep-this intent.
6. `substituted: true` means the canonical content is gone and the operator's
   override registry supplied a replacement — `substituted_ref` is the dead
   canonical ref, `substitution_status` the provenance tier
   (`canonical-recovered` / `captured-original` / `operator-attested` /
   `alternate-master`). Renderers can just play it; anything archival should
   record the distinction.

Caveats:
- Resolved URLs use LAN IPs, never `.local` names — renderers like the FF1
  cannot resolve mDNS.
- Dumb renderers that only take a URL can be pointed at `GET /c?ref=…`
  (302-redirects to the resolved media).

## Seed the box's cache from a wallet

- `POST /seed?ref=<wallet>` where wallet is `0x…`, `name.eth`, `tz1…`, or
  `name.tez`. Enumerates the wallet's NFTs, pins every IPFS ref onto the box,
  and warms the Arweave cache. Returns `202` with a job immediately.
- Poll `GET /seed/{id}` for counts (`tokens`, `pinned`, `recovered`, `warmed`,
  `captured`, `skipped`, `failed`); `GET /seed` lists all jobs since the last
  restart.
- `recovered` = CIDs whose IPFS providers are gone but whose bytes were
  re-fetched from an HTTP copy (the gateway URL in the token metadata),
  re-added, and pinned — only accepted when the hash round-trips to the same
  CID. `failed` therefore means: no IPFS provider AND no faithful HTTP copy.
- `captured` = plain-HTTP media (no content address — the refs most likely to
  vanish) archived into Kubo while the URL still answers, with provenance
  (source, time, size, sha256, new CID) recorded on the box. Captured copies
  are never served automatically; they exist so the operator *can* point a
  dead ref at them later.
- Failures: first 20 in the job's `errors`, complete record in the service
  journal (`journalctl -u content-resolver` on the box).
- Re-running a seed is safe and cheap — pins are idempotent, so a second pass
  just retries the failures. `limit=<n>` runs a partial/test seed.
- `scope=published` (Tezos only) seeds what the wallet *first-minted* instead
  of what it holds. Published works you no longer hold are the most rot-prone
  corner of a collection — holdings-seeding never touches them.
- `scope=created` (Tezos only) seeds what the wallet *authored*
  (`creators`/`authors` metadata) — the robust version of the published sweep,
  without the collected-fxhash noise. Fully-burned creations are skipped unless
  `include_burned=1` (they were destroyed on purpose).
- `scope=contract` seeds every token of one token contract, both chains
  (`ref` must be the literal `0x…`/`KT1…` contract address). This is the ETH
  publication sweep: name the contract the works were minted on.

## Store bytes on the box

- `POST /store` (multipart, REST only — binary doesn't travel over MCP):
  `curl -F file=@master.mp4 'http://<sidecar-ip>:8090/store'` streams the file
  into the box's Kubo, pinned, CIDv1, and records provenance (filename, size,
  sha256, content type, time) in the capture ledger. Returns `cid`,
  `resolved_url`, and the provenance fields.
- Storing bytes serves nothing by itself: a stored CID matters only once an
  override points a dead ref at it. That separation is deliberate.
- **Canonical recovery:** add `?expect_cid=<the dead CID>` and the box pins
  the bytes only if they reproduce that exact CID (`409` otherwise). A match
  resurrects the original CID — it now has a provider again, and NO override
  is needed for that ref at all.
- A `413` means the file exceeds the single-body cap; raise
  `RESOLVER_SEED_RECOVER_MAX_BYTES` on the box if the master really is that big.

## Repair a dead work (override registry)

When a work's canonical media is gone — a CID with no providers, a minted URL
on a dead domain — the operator can point the dead ref at replacement content.
Substitution is never silent: resolve results carry `substituted: true`, the
dead `substituted_ref`, and a provenance tier.

1. Confirm it's dead: `GET /resolve?ref=…` → `resolved: false` (or a
   `resolved_url` that serves nothing).
2. Get the replacement bytes onto the box: `POST /store` for a local file
   (use the returned `cid` as `ipfs://<cid>`), or skip if already pinned.
3. Record the override — `add_override` tool or
   `POST /override` with JSON `{ref, replacement, status, token?, source?,
   captured?, note?}`. `ref` matches ANY spelling of the same content
   (ipfs://CID, /ipfs/CID, gateway URLs, ar://txid, arweave.net). Pick the
   honest `status`: `canonical-recovered` (bytes reproduce the recorded CID),
   `captured-original` (fetched from the canonical URL while it answered),
   `operator-attested` (no hash ever existed; operator stands behind the
   copy), `alternate-master` (different bytes, e.g. a platform HR master).
   Record `token` (e.g. CAIP-19), `source`, and `note` — provenance is the
   point. A duplicate ref returns 409 unless `replace: true`.
4. Verify: `GET /resolve?ref=<the dead ref>` → `substituted: true` and a
   playable `resolved_url`. The POST response's `replacement_resolved` field
   already told you whether the replacement resolves.
5. Inspect or snapshot the registry: `GET /override` (JSON) or
   `GET /override?raw=1` (the TOML file verbatim). The on-box file is the
   source of truth and is machine-managed — hand edits work but comments
   don't survive API writes. `remove_override` / `DELETE /override?ref=…`
   sends a ref back to resolving (i.e. failing) as itself.

## Favorites (the household's picks)

- `GET /favorites` — the browse list, resolved and ready to play: each entry
  carries `ref`, `title`, `note`, `added_at`, plus a live `resolved_url` and
  `playback_method` — hand `resolved_url` straight to a renderer, no separate
  `/resolve` call needed. `resolved: false` marks a pick whose content is
  currently unreachable.
- `POST /favorites?ref=<anything>&note=…` — mark a favorite; `ref` accepts any
  spelling (ipfs://, gateway URL, ar://…) and respellings of the same content
  count as one favorite (duplicate → `409`). Favoriting also makes the bytes
  durable: what the ref resolves to is pinned (IPFS) or cache-warmed
  (Arweave) in the background — browsing/resolving alone never pins;
  favoriting is the keep-this signal.
- `DELETE /favorites?ref=…` — unmark it (any spelling matches); nothing is
  unpinned or deleted.
- MCP: `list_favorites`, `add_favorite(ref, note?)`, `remove_favorite(ref)`.

## Library status

- `GET /library` (MCP: `library_status`) — what the box actually holds, plane
  by plane: IPFS pin count and repo footprint, warmed Arweave txids checked
  live against the gateway cache, and override/favorite/capture counts
  (`null` = that subsystem is disabled).
- Durability is asymmetric: IPFS pins survive GC, but the ar-io cache is
  evictable — `currently_cached` < `known_warmed` means evictions; re-seed or
  resolve with `pin=1` to re-warm.

## Health

- `GET /healthz` — reachability of the box's own IPFS and Arweave gateways.

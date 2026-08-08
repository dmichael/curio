# Curio media resolver specification

Status: target product model. The current implementation does not yet satisfy every invariant in this document.

## Purpose

Curio keeps digital works usable when the references around them are inconsistent, dependent on third parties, or beginning to fail.

A curator gives Curio a wallet, metadata document, or media reference. Curio finds the actual work, hosts it through the appropriate local service, and returns a Curio URL that a player can open.

The central contract is:

```text
reference -> work -> source-appropriate local hosting -> Curio URL
```

Curio does not convert every source into one storage protocol. It preserves the distinction between IPFS, Arweave, and ordinary HTTP while giving callers one consistent resolver and serving origin.

## Product principles

1. Every successful resolution returns a Curio URL. A caller does not need to discover or choose a gateway.
2. Curio serves the resulting work. It does not hand the player back to an unstable source URL.
3. Curio hosts a work through the storage system appropriate to that work: Kubo for IPFS, AR.IO for Arweave, and Curio's static file service for ordinary files.
4. Curio never moves content into IPFS, Arweave, or another protocol without an explicit curator action.
5. Resolution may populate an evictable cache. It does not make the work part of the durable library unless the curator asks to keep it.
6. Keeping has protocol-specific mechanics but one user meaning: retain this work and continue serving it.
7. For decentralized storage Curio supports, keeping also means seeding through the native protocol.
8. Cache state is never described as durable preservation.
9. Replacements are explicit and disclosed. Curio never silently presents different bytes as the canonical work.
10. Wallet ownership, creator attribution, source history, and byte integrity are separate facts.
11. Curio has one external origin. Internal services and ports are implementation details.
12. Curio can run locally, on a private network, or on an internet-accessible host. Its addressing model does not assume a LAN.

## Terms

### Reference

A URI or record that may lead to a work. Examples include an IPFS URI, an Arweave transaction path, an HTTP URL, a `data:` URI, an NFT metadata document, or a Verse artwork page.

### Work

The media a player should open: an image, video, audio file, HTML runtime, or packaged group of files.

A work is not necessarily a single file. Directories, manifests, playlists, and runtime dependencies may be part of it.

### Hosted artifact

The local representation Curio serves through the source-appropriate backend:

- an IPFS DAG pinned or cached in Kubo;
- an Arweave transaction or manifest path cached by the persistent AR.IO Core;
- a static file or package retained or cached by Curio's HTTP file service.

A hosted artifact retains its source kind and native identity. Curio does not treat these backends as interchangeable buckets.

### Resolution record

The active mapping from an input reference to the hosted artifact Curio should serve. It identifies the original reference, source kind, native or local identifier, serving URL, resolution status, and any replacement status.

### Keep state

A hosted artifact is either cached or kept:

- `cached`: available locally now;
- `kept`: explicit source-appropriate keep completed. For Arweave this means
  eager fetch and same-Core cache verification, not a second storage tier.

### Participation

Serving protocol-native content beyond the curator's own clients. IPFS participation means providing pinned DAGs to peers. Arweave participation means operating a useful AR.IO gateway that re-serves retained data.

For decentralized storage, participation is part of Curio's standard behavior. Running a daemon is not sufficient evidence of participation; Curio should report whether it is reachable and actually serving data when the protocol exposes those facts.

## Resolution contract

Resolution follows intermediate references until it reaches playable media.

Examples:

- NFT metadata is read and its animation, artifact, or image field is followed.
- An IPFS gateway URL is reduced to its CID and path.
- An Arweave manifest path is followed without discarding the path.
- A Verse page is inspected for its token URI, iframe, or original artwork image.
- A `data:application/json` URI is decoded and treated as metadata.
- A direct HTTP URL is fetched and served by Curio's static service rather than returned unchanged.

A successful response includes at least:

```json
{
  "original_ref": "...",
  "media_url": "https://curio.example/media/...",
  "source_kind": "http",
  "media_type": "video/mp4",
  "playback_method": "play",
  "keep_state": "cached",
  "integrity": {
    "algorithm": "sha256",
    "digest": "..."
  },
  "substituted": false
}
```

`media_url` is always on the Curio origin used by the client. Curio derives that origin from the request or trusted proxy headers; it does not require a configured LAN address.

An unresolved response says why Curio could not produce a locally hosted work. It must not return an upstream URL and call that success.

## Native and static serving

Curio uses one external origin while routing each work to its proper backend.

```text
https://curio.example/ipfs/<cid>/<path>       -> Kubo
https://curio.example/arweave/<txid>/<path>  -> AR.IO
https://curio.example/media/<file-id>        -> Curio static file service
```

An IPFS work remains its original DAG and is served by Kubo. Curio must not flatten a directory or file into a static copy and claim that the original CID was preserved.

An Arweave work remains associated with its original transaction and manifest identity and is served by the one persistent AR.IO Core. Resolve, play, and keep all use that same cache; there is no fallback or retained tier.

HTTP files, inline media, and operator uploads are served as static HTTP files. They do not enter Kubo merely because Kubo is running. Publishing one of these files to IPFS is a separate, explicit action that creates a new CID and a new source identity.

If a backend is unavailable, Curio reports degraded serving. It does not silently move the work to another protocol.

## Cache and keep

Resolution and keep use the same source-appropriate backend.

### Cache

On first successful resolution, Curio fetches enough of the work into the appropriate local cache to serve it through Curio.

- IPFS blocks may enter Kubo's cache without being pinned.
- Arweave data may enter the persistent AR.IO Core cache through resolve or play.
- HTTP and inline media may enter Curio's evictable static file cache.

Core's automatic content cleanup is disabled. Curio may still need to fetch again if local state is unavailable or maintenance removed invalid cache references.

### Keep

A keep request applies source-appropriate completion semantics in the same backend.

- IPFS: pin the canonical DAG in Kubo and provide it to the network.
- Arweave: fully fetch the transaction/path and verify local availability through the same AR.IO Core.
- HTTP or inline media: retain the static file or package in Curio's HTTP store.
- Operator upload: retain the uploaded static file in Curio's HTTP store.

Keeping is idempotent. Repeating the request for the same source identity and immutable bytes does not create another logical work.

For IPFS, keep also means seed. Arweave keep verifies local Core cache availability only; it is not an Arweave-network replication claim.

Keep intent can come from:

- an explicit keep action on one reference;
- adding a favorite when the curator has chosen favorites as keep intent;
- seeding selected works from a wallet, creator listing, or contract;
- an operator upload;
- a collection policy configured by the curator.

## Source adapters

Every adapter ends in its appropriate local serving backend.

| Source | Resolution | Keep | Serving |
|---|---|---|---|
| IPFS | Parse CID/path, fetch and verify the requested DAG or file | Pin the original DAG and announce it | Kubo IPFS gateway and IPFS peer service |
| Arweave | Resolve transaction and manifest paths through the one Core | Fully fetch and verify a same-Core native cache hit | AR.IO gateway |
| HTTP(S) | Fetch through Curio, validate redirects, and determine whether the result is metadata or media | Retain the static file or package in Curio's HTTP store | Curio static file service |
| `data:` | Decode inline metadata or media | Retain decoded media in Curio's HTTP store | Curio static file service |
| NFT metadata | Follow fields to the selected media and retain token context when supplied | Keep the final work in the backend selected by its media reference | Backend selected by the final media reference |
| Verse and similar pages | Extract the token URI, iframe, or artwork media | Keep the final work and any package needed to run it | Backend selected by the final media reference |
| Operator upload | Accept a static file directly | Retain it in Curio's HTTP store | Curio static file service |

Cross-protocol publication is never implicit. An operator may explicitly publish a static HTTP file to IPFS, but the resulting CID is an additional identity rather than a transparent storage detail.

## Network participation

The philosophy is simple: for works a curator owns or values, retrieval without contribution is insufficient. Curio should not leech from decentralized storage while leaving preservation to everyone else. Seeding adds an independent participant and makes the work more likely to remain available.

No single Curio instance can guarantee survival. Survival comes from durable copies and independent participants. Curio's responsibility is to contribute rather than assume that others will.

The standard Curio installation enables both Kubo and AR.IO.

### IPFS

Kept IPFS works are pinned as their canonical DAGs. Kubo announces and serves those blocks to peers. The default configuration should support meaningful reachability through direct inbound access, relays, hole punching, or explicit peering as appropriate to the deployment.

### Arweave

Arweave works remain available through Curio's one pinned AR.IO Core and retain
the original transaction/manifest path identity. Resolve and playback can
populate its persistent cache. Explicit keep fully consumes the exact Core
response and then fully consumes a second response requiring native `X-Cache:
HIT`; native cold reads use a configurable 300-second default timeout. Core
uses embedded LMDB and disables automatic chunk-data cleanup.

This is same-Core eager fetch/verification, **not** an AR.IO pin API, movement
between tiers, or a claim of new replication in the Arweave storage network.
AR.IO gateway serving does not create a new Arweave transaction.

### Reduced operation

An operator may disable Kubo or AR.IO for development or severe resource constraints, but reduced operation is explicit and visible in health and library status. It is not the standard Curio posture.

Curio reports actual contribution rather than the mere presence of a daemon. A node hidden behind an unreachable boundary may improve local retrieval but should not be reported as serving the wider network.

## Static files, packages, and runtime works

A single image, video, or audio file can be stored and served as one static file.

A directory, manifest, HLS presentation, or HTML work may depend on several files. Curio needs a package representation that preserves paths and identifies the package entry point. Packages from ordinary HTTP remain HTTP-served packages; they are not automatically converted into IPFS DAGs.

Runtime HTML is not preserved by saving one HTML response. Scripts, styles, media, fonts, workers, API calls, and origin behavior may all be required. Until Curio can capture and replay those dependencies, it must report the work as live-dependent rather than claim that it is kept.

A live reverse proxy can make a runtime work playable through Curio, but proxying alone is not preservation.

## Replacements and dead sources

When a canonical source is unavailable, Curio may serve a retained artifact from the same backend or an operator-approved replacement.

For content-addressed references, bytes are canonical only when they reproduce the recorded identifier.

For unhashed HTTP sources, a copy fetched from the canonical URL while it was live can be labelled `captured-original`. This states what Curio observed and when; it does not invent cryptographic proof that the source never changed.

Other replacements require an explicit curator decision and one of the existing evidence labels:

- `operator-attested`
- `alternate-master`

A replacement can use a different backend only because the curator explicitly chose it. Curio records and discloses that change; it never presents cross-protocol movement as an invisible implementation detail.

Every substituted response includes the dead reference, replacement status, and selected hosted artifact. The active replacement mapping can remain small and operator-readable.

## Minimal source records

Complete provenance history is a useful north star, not a requirement for the first consistent resolver.

Curio does need enough information to explain and operate each kept artifact:

- original input reference;
- final source reference;
- source kind and native identifier when one exists;
- local serving identifier;
- integrity digest where Curio can compute or verify one;
- byte length and media type;
- retrieval time;
- cache or keep state;
- wallet, chain, contract, token ID, and metadata field when that context is available;
- replacement status and operator note when applicable.

A wallet records how a work was discovered or selected. It is not proof of authorship, authenticity, or canonical bytes.

These records must not claim transactional guarantees that the implementation does not provide. If backend retention and metadata storage are separate operations, ingestion needs pending and completed states plus reconciliation after interruption.

A comprehensive append-only provenance history can be added later without changing the resolution, serving, and keep contracts above.

## Storage backends

Curio has distinct storage backends with a shared retention vocabulary rather than one generic object bucket.

Each backend provides the operations its source type needs:

```text
resolve(reference) -> hosted artifact
serve(hosted artifact) -> stream
cache(hosted artifact)
keep(hosted artifact)
release(hosted artifact)
status(hosted artifact) -> cached | kept | missing
```

The IPFS backend delegates canonical DAG storage and serving to Kubo. The Arweave backend delegates transaction-aware storage and serving to AR.IO. The static backend stores ordinary files and packages without assigning them an IPFS or Arweave identity.

Metadata such as source mappings, keep state, and replacements belongs in a transactional local database. Flat JSON or JSONL files may be useful exports, but they are not the authoritative store when correctness depends on coordinated updates.

## Wallets and curation

Wallets are discovery inputs. Curio currently targets Ethereum mainnet and Tezos mainnet inventory through public indexers.

A curator can select held works, creator-attributed works, first-minted works, or a contract catalog where the chain and indexer support that query.

Selection does not prove authorship. Creator metadata, current ownership, first minting, and curator intent remain separate fields.

Curio does not need a permanent marketplace index. It may query indexers live, then retain only the work references and context needed for artifacts the curator caches or keeps.

## Deployment model

Curio does not assume a LAN address and does not require `CURIO_LAN_ADDRESS` or an equivalent setting.

A client already knows the Curio origin it contacted. Returned URLs use that origin. A configured public base overrides it. Reverse-proxy deployments may provide the external origin through RFC `Forwarded` or `X-Forwarded-Proto` plus `X-Forwarded-Host`, but only when the immediate proxy IP/CIDR is explicitly allowlisted; direct-client and malformed/partial forwarded headers are ignored.

The public installer must not require `sudo`. A per-user installation can place application files, configuration, and state under XDG paths and use Docker or another container runtime available to that user.

A running instance exposes its installed version and release identity. Operator-driven updates use verified release artifacts:

```text
curio version
curio update --check
curio update
curio update --version vX.Y.Z
```

Automatic installation of updates is not required. A preservation service should not replace itself without curator approval.

## Security model

Curio may run outside a private LAN. Network location is not an authorization mechanism.

Read-only media serving and resolver access may be public. Actions that change retention or routing require curator authorization, including keep, release, upload, replacement, cross-protocol publication, and configuration operations.

Source fetching requires SSRF controls that resolve DNS before connection, reject prohibited address ranges, and revalidate every redirect target. Limits apply to body size, concurrency, time, and total work per request.

TLS may terminate in Curio's front door or a trusted reverse proxy. Internal protocol services are not exposed directly except through intentionally enabled participation endpoints.

## Non-goals

Curio is not a marketplace, wallet custody service, ownership oracle, creator registry, or guarantee that every runtime can be reconstructed.

Curio does not treat a public gateway response as durable simply because it was once cached.

Curio does not silently alter a work, move it between protocols, or hide a replacement from callers.

## Acceptance invariants

An implementation conforms to this model when:

1. Every successful resolution returns a URL on the Curio origin.
2. Every served artifact comes from the backend appropriate to its source kind.
3. IPFS content is hosted and served by Kubo under its canonical CID and path.
4. Arweave content is hosted and served by AR.IO under its canonical transaction and manifest identity.
5. HTTP, inline, and uploaded files are hosted and served by Curio's static HTTP service unless the curator explicitly publishes them elsewhere.
6. No content crosses into IPFS, Arweave, or another protocol without explicit curator intent.
7. Every keep action uses its source-appropriate operation or clearly fails.
8. Arweave keep reports only completed same-Core fetch/verification; it does not claim a separate durable tier after restart.
9. HTTP kept works survive loss of their upstream source; Arweave cache availability is local Core state, not an upstream or network replication guarantee.
10. Content-addressed sources are verified against their recorded identifier when claiming canonical preservation.
11. Replacements and cross-protocol publication are explicit in stored records and resolution responses.
12. Internal service addresses never appear in consumer responses.
13. Deployment does not require a configured LAN address or root installation.
14. Kubo and AR.IO participation is enabled by default, and status reports actual network contribution rather than the mere presence of a daemon.

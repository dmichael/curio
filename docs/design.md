# Curio design

## Contract

Curio turns a reference into locally hosted media:

```text
reference -> work -> source-appropriate local hosting -> Curio URL
```

Every successful resolution returns `media_url` on the Curio origin. It never
calls an upstream URL a successful playable result. One public origin routes
`/ipfs/...` to Kubo, `/arweave/...` to AR.IO, and `/media/...` to Curio's static
store. Internal service names and ports do not appear in consumer responses.

## Storage planes and retention

Curio does not flatten every work into IPFS.

- **IPFS:** resolution uses Kubo's cache and serving keeps the original
  CID/path. Explicit keep pins the canonical DAG in Kubo, which is Curio's
  IPFS contribution to the network.
- **Arweave:** ordinary reads use the evictable r81 Core data plane. Explicit
  keep records `pending`, hydrates a private retained r81 Core with independent
  persistent state, verifies a second native cache hit, then records `kept`.
  Kept paths route only to that retained Core; an unavailable retained plane is
  degraded, not silently replaced with ordinary-cache bytes. This is isolated
  native retained-plane operation, not an AR.IO r81 pin API and not new
  replication in the Arweave storage network.
- **HTTP, `data:`, and uploads:** Curio stores and serves bounded static files
  under `/media/<id>`, with a SHA-256 record. They never enter Kubo implicitly.
  Cross-protocol publication would need an explicit curator action and a new
  identity; it is not implemented by the current API.

`cached` and `kept` are different states. Resolution may cache media. Explicit
keep, favorite intent, an operator upload, or a seed promotes the final artifact
on its own plane. Runtime HTML is `live-dependent`: capturing one response does
not preserve dependencies, workers, APIs, or origin behavior, so keep refuses
it rather than making a preservation claim.

## Resolver behavior

Curio recognizes IPFS and gateway spellings, Arweave transaction/manifest
paths, HTTP(S), `data:` URIs, token metadata, UnixFS wrappers, and Verse
artwork pages. Metadata recursion selects animation/artifact fields first and
otherwise chooses the largest probeable image candidate. Bare CID responses may
add a filename hint for renderers such as the Feral File FF1 that infer playback
from URL suffixes. Static media uses `play`; HTML uses `send`.

A direct HTTP media response is fetched with SSRF checks, copied into static
storage, and returned as a Curio URL. A direct `data:` media response is decoded
into the same store. Source bytes are not returned as a success until Curio can
serve them locally. A failed resolution says why.

The current resolver does not accept a chain contract/token pair as `/resolve`
input or query a chain RPC for its token URI.

## Curation, provenance, and chains

`/wallet` reads live public indexers; it is discovery, not an ownership,
authorship, or authenticity oracle. `/seed` is an authenticated background job
that keeps final artifacts source-appropriately. Ethereum mainnet uses
Blockscout/BENS for ERC-721/ERC-1155 holdings, ENS, and contract listings.
Tezos mainnet uses TzKT for FA2 holdings, `.tez` names, first-minted,
creator-attributed, and contract listings. Tezos `published` is first-minter
history, not authorship; `created` uses creator/author metadata. Ethereum has
no reliable keyless creator index.

An override maps a dead canonical reference to an operator-selected replacement
and is always disclosed as `substituted` with a provenance status:
`canonical-recovered`, `captured-original`, `operator-attested`, or
`alternate-master`. A wallet is discovery context, not proof of creator or
canonical bytes.

## Network, trust, and participation

The Compose graph has one HTTP ingress, resolver port `8090`; Kubo, ordinary
AR.IO Core, retained AR.IO Core, Redis, and Envoy gateway/admin interfaces are
private. Kubo swarm `4001/tcp` and `4001/udp` are published for native IPFS
participation. Kubo and AR.IO are enabled by default. `/healthz` distinguishes
backend health from participation evidence: advertised Kubo addresses are not
an inbound reachability probe, and r81 exposes no equivalent AR.IO reachability
fact, so both can honestly remain `unknown`.

Read-only routes may be public. Mutations require the curator bearer token:
keep/pin, seed, upload, favorite changes, and override changes. Source fetching
checks literal addresses, DNS results, and each redirect target before
connection, then applies bounded body, concurrency, and timeout limits.

For direct HTTP, the origin is derived from the request. A proxy deployment can
set `CURIO_PUBLIC_BASE_URL` for its external origin. Forwarded-header trust is
not implemented at this revision; `CURIO_TRUSTED_PROXY_HEADERS` is not a
supported setting. The target model calls for opt-in trusted proxy handling, so
this is an implementation gap rather than an implied security feature.

## Operations

The supported source install is per-user and no-sudo. It uses
`$XDG_CONFIG_HOME/curio/curio.env`, `$XDG_DATA_HOME/curio/app/releases`, and
`$XDG_DATA_HOME/curio/state` by default. The state tree contains Kubo data,
ordinary and retained AR.IO state, static media, and operator records; back up
those actual paths.

`curio version`, `curio update --check`, `curio update`, and
`curio update --version vX.Y.Z` exist as operator commands. The installer uses
an atomic `current` release symlink and restores the prior graph after failed
health. The remote bootstrap verifies a release archive checksum before running
it, but no release asset is currently published and the source-install update
path does not yet fetch/select verified releases. Operators must not infer an
automatic or already-available release updater.

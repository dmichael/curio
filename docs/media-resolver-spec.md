# Curio media behavior

This document describes the product Curio implements today. It is not a backlog
or an architecture wish list.

## What Curio does

Curio accepts a media reference, follows metadata when necessary, and returns a
URL on the Curio server that handled the request.

```text
reference -> media -> Curio URL
```

Curio keeps the original storage protocol where that matters:

| Source | Local service | Curio path |
|---|---|---|
| IPFS | Kubo | `/ipfs/<cid>/<path>` |
| Arweave | AR.IO Core | `/arweave/<txid>/<path>` |
| HTTP, `data:`, upload | Curio static store | `/media/<id>` |

A successful resolution never returns an external gateway URL as the playable
result.

## Resolution

`GET /resolve?ref=...` recognizes:

- IPFS URIs, `/ipfs/` paths, and gateway URLs;
- Arweave transaction and manifest paths;
- HTTP media and JSON metadata;
- `data:` metadata and media;
- small UnixFS directory wrappers;
- Verse artwork pages.

Metadata is followed until Curio finds media it can serve. IPFS and Arweave
retain their CID or transaction identity. Ordinary HTTP and inline bytes are
copied into Curio's bounded static cache.

HTML is marked `live-dependent`. Saving one HTML response does not preserve its
scripts, APIs, workers, fonts, or other network dependencies.

## Local storage

### IPFS

Kubo fetches and serves IPFS content. Resolution may populate its cache. An
explicit keep pins the CID root so Kubo retains and seeds the DAG.

### Arweave

Curio runs one AR.IO Core with persistent state. Resolve, playback, and keep all
use this same Core. Automatic content cleanup is disabled, so content fetched by
Core is left in its local store.

An explicit Arweave keep forces a complete fetch and checks that Core can serve
the same transaction or manifest path as a cache hit. It does not move data to a
second tier. It also does not create a new Arweave transaction or prove that the
Arweave network gained another replica.

### Static media

Curio stores HTTP, inline, and uploaded files by SHA-256. Ordinary resolution
uses a size-bounded LRU cache. Explicitly kept files and uploads are not evicted
by that cache.

Curio does not add these files to IPFS automatically.

## Keep

`POST /keep?ref=...` is synchronous:

- IPFS: pin the CID root;
- Arweave: fully fetch and verify the same AR.IO Core cache;
- HTTP or `data:`: mark the static object as kept.

`GET /resolve?ref=...&pin=1` is an older convenience form. IPFS pinning runs in
the background there, so `pin_scheduled` is not completion proof.

Wallet-wide keeping uses `POST /seed?ref=...` and reports progress through
`GET /seed/<job-id>`.

## Replacements

An override can map a dead reference to a curator-selected replacement. Every
substituted response says that a replacement was used and includes its status.
Curio does not silently call different bytes canonical.

The supported statuses are:

- `canonical-recovered`
- `captured-original`
- `operator-attested`
- `alternate-master`

## Origin and access

Curio has one public HTTP origin for REST, MCP, static media, IPFS, and
Arweave. Internal Kubo and AR.IO addresses are not returned to clients.

Returned URLs normally use the request origin. `CURIO_PUBLIC_BASE_URL` can set
an explicit origin. Forwarded headers are accepted only from an immediate proxy
listed in `CURIO_TRUSTED_PROXY_CIDRS`.

Read-only routes may be public. Keep, seed, uploads, favorites, and override
changes require the curator bearer token.

## Wallet discovery

Curio reads Ethereum mainnet inventory through Blockscout and BENS, and Tezos
mainnet inventory through TzKT. Wallet results are discovery data, not proof of
authorship or authenticity.

Curio does not currently resolve a contract/token pair by querying a chain RPC.

## Deployment

The default appliance runs three services: Curio, Kubo, and one AR.IO Core. It
installs under per-user XDG paths and does not require `sudo`.

The public HTTP port is `8090` by default. Kubo also publishes swarm port 4001
for IPFS peers. Kubo's API and gateway and AR.IO Core remain private to the
Compose network.

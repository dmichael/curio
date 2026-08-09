# Design

Curio turns curator intent into stored, playable media on one origin.

```text
                         +-> Kubo --------> /ipfs/...
POST /resolve ----------+-> AR.IO Core --> /arweave/...
                         +-> static store -> /media/...
```

Only the resolver is a public HTTP service. Kubo and AR.IO remain behind it.
The rationale for this three-service, source-native shape is recorded in
[architecture decision 0001](decisions/0001-three-service-source-native-architecture.md).

## Resolution is storage

Curio is not a neutral gateway with an optional preservation action. A
successful `POST /resolve` means Curio resolved the submitted reference and
stored its final artifact.

Curio writes one SQLite resolution record:

```text
submitted reference -> normalized lookup key -> final reference -> media path
```

The submitted reference remains the public identifier. IPFS and Arweave gateway
spellings normalize to their native identities; ordinary HTTP URIs remain
distinct. Static bytes are independently deduplicated by SHA-256.

`GET /resolve?ref=...` performs only a lookup. A known reference redirects to
its `/ipfs`, `/arweave`, or `/media` path; an unknown reference returns 404.
How clients browse the contents of Curio is a separate concern.

Statuses are `ready`, `live-dependent`, and `failed`. HTML is
`live-dependent`: Curio stores the primary artifact without claiming to have
captured every script, API, worker, font, or other runtime dependency.

## Three storage paths

IPFS content stays under its original CID in Kubo. POST resolution pins the CID
root.

Arweave content stays under its transaction and manifest identity. Curio uses
one persistent AR.IO Core with automatic content cleanup disabled. POST
resolution fully fetches the selected artifact and verifies the same local Core.
This is local storage, not a claim that Curio added a replica to the Arweave
network.

HTTP, `data:`, and uploaded media use Curio's static store. Stored objects are
not evicted. Curio does not silently convert ordinary files to IPFS.

## Curation

Wallet endpoints use public Ethereum and Tezos indexers to discover references.
They do not prove ownership history, authorship, or authenticity. A seed job is
the batch form of storage intent for a selected wallet or contract.

Overrides are explicit mappings for dead references. Substituted results carry
the override status so clients can distinguish a recovered canonical object
from an operator-selected alternative.

Favorites organize selected references; they do not define separate storage
semantics.

## State

The default state directory contains:

```text
ipfs/          Kubo repository and pins
ar-io/         AR.IO Core data, LMDB, and SQLite state
media/         static objects, media records, and resolution records
overrides.toml operator replacements
favorites.json household selections
```

Back up this directory and `curio.env`.

## Network model

Curio is designed for a trusted household or studio network. It has no user
authentication: any client that can reach the service can submit media, seed
wallets, upload files, and modify operator records. Do not expose it directly to
the public internet.

The resolver serves REST, MCP, and media on port 8090. Kubo publishes port 4001
for IPFS peers. Other service ports stay private. Returned media URLs use the
request origin unless an explicit public base or trusted proxy origin is
configured.

Curio still bounds remote fetches and rejects private network targets because
NFT metadata and remote servers are not trusted merely because the caller is.

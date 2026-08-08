# Design

Curio gives players one URL space for media stored in several different ways.

```text
                         +-> Kubo --------> /ipfs/...
request -> resolver -----+-> AR.IO Core --> /arweave/...
                         +-> static store -> /media/...
```

Only the resolver is a public HTTP service. Kubo and AR.IO remain behind it.

## Three storage paths

IPFS content stays in Kubo under its original CID. Keeping it pins the CID root.

Arweave content stays under its transaction and manifest identity. Curio uses
one persistent AR.IO Core with automatic content cleanup disabled. Any Arweave
content fetched during resolution or playback can remain in that store. The
Arweave keep action forces a full fetch and verifies a local cache hit; there is
no separate retention service.

HTTP, `data:`, and uploaded media use Curio's static store. The ordinary cache
has a size limit and evicts only unkept entries. Curio does not silently convert
these files to IPFS.

## Resolution

The resolver understands common IPFS and Arweave forms, HTTP and inline
metadata, UnixFS directory wrappers, and Verse pages. It follows metadata to the
selected media and returns a URL on the Curio origin.

HTML can depend on resources that Curio has not captured. Such results are
marked `live-dependent` instead of kept.

## Curation

Wallet endpoints use public Ethereum and Tezos indexers to find references.
They do not prove ownership history, authorship, or authenticity. A seed job
keeps the final media found for the selected wallet or catalog.

Overrides are explicit mappings for dead references. Substituted results carry
the override status so clients can distinguish a recovered canonical object
from an operator-selected alternative.

## State

The default state directory contains:

```text
ipfs/          Kubo repository and pins
ar-io/         AR.IO Core data, LMDB, and SQLite state
media/         static objects and catalogue
overrides.toml curator replacements
favorites.json curator selections
```

Back up this directory and `curio.env`. Arweave's local cache is useful, but it
is not a claim that Curio added data to the Arweave storage network.

## Network and authorization

The resolver serves REST, MCP, and media on port 8090. Kubo publishes port 4001
for IPFS peers. Other service ports stay private.

Read-only routes may be public. Mutations require the curator token. Returned
media URLs use the request origin unless an explicit public base or trusted
proxy origin is configured.

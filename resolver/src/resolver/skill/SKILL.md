---
name: curio
description: Resolve NFT media through Curio and keep selected IPFS, Arweave, HTTP, data, or uploaded works.
---

# Curio

Curio resolves media references and returns URLs on the Curio server. Use
`GET /openapi.json` for the full schema.

## Resolve

```text
GET /resolve?ref=<reference>
GET /c?ref=<reference>
GET /wallet?ref=<wallet>
GET /favorites
GET /library
GET /healthz
```

References may be IPFS, Arweave, HTTP, `data:` metadata or media, or a Verse
artwork page. A successful result contains `media_url` and `resolved_url` on the
Curio origin.

Curio serves IPFS at `/ipfs/...`, Arweave at `/arweave/...`, and ordinary files
at `/media/...`. `playback_method` is `play` for static media and `send` for
HTML. `live-dependent` HTML may still need uncaptured network resources.

A substituted result names the replacement and its status.

## Authenticate changes

REST mutations require:

```text
Authorization: Bearer <CURIO_CURATOR_TOKEN>
```

MCP mutation tools take the same value as `curator_token`.

## Keep one work

```text
POST /keep?ref=<reference>
```

- IPFS keep pins the CID root in Kubo.
- Arweave keep fully fetches and verifies the same persistent AR.IO Core used
  by resolve and playback.
- HTTP and `data:` keep marks the static object as kept.

`GET /resolve?ref=...&pin=1` is a compatibility shortcut. IPFS pinning is
asynchronous there; `pin_scheduled` does not mean it finished.

Adding a favorite also expresses keep intent. Removing a favorite does not
delete media.

## Keep a wallet or catalog

```text
POST /seed?ref=<wallet>&scope=held
GET /seed/<job-id>
```

Supported scopes are `held`, `published`, `created`, and `contract`, subject to
chain support. Ethereum mainnet uses Blockscout and BENS. Tezos mainnet uses
TzKT. `published` is Tezos first-mint history; `created` uses Tezos
creator/author metadata. Ethereum has no keyless creator index.

Seed jobs keep final IPFS, Arweave, and static artifacts through their existing
local services. Job history is in memory.

## Upload and replace

```text
POST /store                  multipart field: file
POST /override               JSON body
DELETE /override?ref=...
POST /favorites?ref=...
DELETE /favorites?ref=...
```

Uploads stay in Curio's static store and return a `/media/...` URL. They do not
produce an IPFS CID.

Overrides use one of these statuses:

- `canonical-recovered`
- `captured-original`
- `operator-attested`
- `alternate-master`

Curio reports every override as a substitution.

## Notes

AR.IO Core keeps fetched content in one persistent cache with automatic content
cleanup disabled. Arweave keep is a forced download and local cache check, not a
new Arweave replica.

HTTP and inline media never enter IPFS automatically. Runtime HTML is not fully
preserved unless its dependencies are also captured.

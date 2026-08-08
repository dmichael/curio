---
name: curio
description: Resolve media references into playable URLs on the Curio origin; explicitly keep selected IPFS, Arweave, HTTP, data, or uploaded works with curator authorization. Fetch this skill from GET /skill/SKILL.md on a running Curio for the service's instructions.
---

# Curio — agent instructions

Curio resolves a reference to a locally served work. A successful response has
`media_url` (and the compatibility field `resolved_url`) on the Curio origin;
never send the caller to an upstream gateway as a successful result.

Use `GET /openapi.json` or `/docs` for exact schema details. This skill is
served by the box at `GET /skill/SKILL.md`. The collector playbook is at
`GET /skill/nft-preservation`.

## Read-only resolution

```text
GET /resolve?ref=<reference>
GET /c?ref=<reference>        # 302 for renderers that only accept a URL
GET /wallet?ref=<wallet>
GET /library
GET /healthz
GET /favorites
GET /override
```

`ref` may be an IPFS URI/path/gateway URL, `ar://txid[/path]`, an Arweave
URL, HTTP(S), token metadata, on-chain `data:` metadata/media, or a Verse
artwork page. `media_url` is always on the request's Curio origin for HTTP
calls. Hand it to a renderer exactly as returned; filename query hints are
functional for extension-sniffing renderers such as the Feral File FF1.

`playback_method: "play"` means static media; `"send"` means HTML. A runtime
HTML result is `live-dependent`, not a claim that Curio preserved its scripts,
assets, APIs, workers, or origin behavior. `resolved: false` means no local
artifact could be served. `substituted: true` discloses an operator replacement
and includes `substituted_ref` and `substitution_status`.

Curio uses `/ipfs/<cid>/<path>` through Kubo, `/arweave/<txid>/<path>` through
AR.IO, and `/media/<id>` for HTTP/data/uploads. HTTP, inline data, and uploads
never enter IPFS implicitly.

## Explicit keep and authentication

Mutations require `Authorization: Bearer <CURIO_CURATOR_TOKEN>`. Read-only
routes may be public. Use an explicit keep when preservation, rather than a
cache hit, is intended:

```bash
curl -X POST -H 'Authorization: Bearer YOUR_TOKEN' --get \
  'https://curio.example/keep' \
  --data-urlencode 'ref=ipfs://bafy.../work.mp4'
```

`POST /keep?ref=...` returns only after its source-appropriate promotion:

- IPFS pins the canonical DAG in Kubo and seeds it.
- Arweave fully fetches and verifies the same persistent Core cache used for
  resolve/play. It is not an AR.IO pin API or new Arweave replication.
- HTTP, `data:`, and uploads promote the existing Curio static object.

`GET /resolve?ref=...&pin=1` is an authenticated convenience action. For IPFS
it schedules a background pin and reports `keep_state: "pending"`; that is not
proof it completed. Static and Arweave promotions report their result in the
response. Do not use `pin=1` for runtime HTML: it remains `live-dependent`.

Favorites are also explicit curator intent:

```text
POST   /favorites?ref=<reference>&note=<optional>
DELETE /favorites?ref=<reference>
```

A favorite promotes a final static or Arweave artifact immediately, schedules
an IPFS pin, and does not make an HTML runtime preserved. Removing a favorite
does not release bytes.

## Wallet discovery and seeding

`GET /wallet?ref=<wallet>` reads live inventory. `ref` accepts `0x...`,
`name.eth`, `tz1...`, or `name.tez`; use `scope=held|published|created|contract`
as supported by the chain. Ethereum mainnet discovery uses Blockscout/BENS;
Tezos mainnet uses TzKT. `published` is Tezos first-mint history, not authorship;
`created` is Tezos creator/author metadata. Ethereum has no keyless creator
index. Other chains are not supported for wallet discovery.

Start an authenticated whole-wallet keep job with:

```bash
curl -X POST -H 'Authorization: Bearer YOUR_TOKEN' --get \
  'https://curio.example/seed' \
  --data-urlencode 'ref=name.eth' \
  --data-urlencode 'scope=held'
```

It returns `202` and a job id; poll `GET /seed/<id>` or list `GET /seed`.
Seeding pins IPFS final artifacts, fetches/verifies Arweave final artifacts
through the same Core, and promotes ordinary HTTP/data final artifacts in static
storage. It does not move ordinary bytes into Kubo. Job history is in memory;
kept media survives restart, job status does not.

## Uploads, overrides, and status

Upload an operator-supplied static file:

```bash
curl -X POST -H 'Authorization: Bearer YOUR_TOKEN' \
  -F 'file=@master.mp4' 'https://curio.example/store'
```

`POST /store` returns `id`, `media_url`, SHA-256 integrity, and
`source_kind: "upload"`; it stores the file as kept Curio static media. It does
not produce a CID and has no `expect_cid` parameter. Adding an upload does not
make it a replacement by itself.

Manage a disclosed replacement with authenticated `POST /override` (JSON body
with `ref`, `replacement`, and `status`) or `DELETE /override?ref=...`.
Statuses are `canonical-recovered`, `captured-original`, `operator-attested`,
and `alternate-master`. A replacement is never silent.

`GET /library` separates Kubo pin status, same-Core Arweave cache diagnostics,
and operator records. Resolve/play also populate that cache; explicit keep is
an eager fetch/verification, not a replication claim. `/healthz`
reports backend health plus conservative participation evidence; AR.IO public
reachability can honestly be `unknown`.

## Origin and MCP

Connect streamable HTTP MCP at `/mcp`. MCP mutation tools take the curator
token as `curator_token`; REST uses the bearer header. Direct HTTP URLs derive
from the request origin. `CURIO_PUBLIC_BASE_URL` explicitly overrides that
origin for proxy or non-request MCP deployments. Otherwise forwarded headers
are ignored unless `CURIO_TRUSTED_PROXY_CIDRS` allowlists the immediate proxy's
IP/CIDR range. An allowlisted peer can provide a complete valid RFC `Forwarded`
origin or `X-Forwarded-Proto` plus `X-Forwarded-Host`; malformed or partial
values are ignored. Do not allowlist client networks.

# Curio media behavior

This document describes the product Curio implements today.

## Store

`POST /resolve` accepts either a media reference or a multipart file. A
successful request resolves and stores the final playable artifact, then returns
its submitted reference, final source identity, media URL, and status.

Reference request:

```http
POST /resolve?ref=ipfs://bafy.../artwork
```

JSON and multipart bodies are also accepted:

```json
{"ref":"https://example.com/token/42.json"}
```

```text
file=@master.mp4
```

Status is one of:

- `ready`: stored and playable through Curio;
- `live-dependent`: the primary HTML artifact is stored, but uncaptured network
  dependencies may remain;
- `failed`: Curio could not complete the request.

Failed submissions are not registered for playback.

## Play

`GET /resolve?ref=...` looks up a previously successful submission. It redirects
to the recorded source-native path or returns 404 when the reference is unknown.
GET performs no external resolution and expresses no new storage intent.

Equivalent IPFS and Arweave gateway spellings share a lookup key. Ordinary HTTP
URIs remain distinct identities. Multiple HTTP URIs may point to one deduplicated
SHA-256 object without becoming the same reference.

## Reference adapters

POST resolution recognizes:

- IPFS URIs, `/ipfs/` paths, and gateway URLs;
- Arweave transactions and manifest paths;
- HTTP media and JSON metadata;
- `data:` metadata and media;
- small UnixFS directory wrappers;
- Verse artwork pages.

Metadata and wrappers are followed to the selected final artifact. Overrides
are checked during recursion and substitutions are disclosed in the POST
response.

Verse artwork pages resolve chain-first: the page's embedded contract address
and token id drive an ERC-721 `tokenURI` (or ERC-1155 `uri`) call over
`RESOLVER_ETH_RPC_URL`, and the returned tokenURI is resolved like any other
metadata reference. Only when on-chain resolution is impossible — no
coordinates on the page, RPC disabled or unreachable, or the chain-found
metadata unreachable — does it fall back to scraping the page directly
(embedded `tokenUri` / `iframeUrl` / `og:image`). A chain-found canonical ref
that turns out to be dead is still disclosed in the response `note`, even
when a scrape fallback is what actually plays.

## Source-native storage

| Final source | Storage action | Playback path |
|---|---|---|
| IPFS | Pin the CID root in Kubo | `/ipfs/<cid>/<path>` |
| Arweave | Fully fetch and verify through the persistent AR.IO Core | `/arweave/<txid>/<path>` |
| HTTP, `data:`, upload | Store by SHA-256 in Curio | `/media/<id>` |

Curio does not automatically add ordinary files to IPFS. AR.IO storage is local
appliance state, not a claim of new Arweave-network replication.

## Batch storage

`POST /seed?ref=...` discovers wallet or contract media and stores final
artifacts on their source-native planes. Seed jobs run in the background and are
polled through `GET /seed/<job-id>`.

## Replacements

An override can map a dead reference to an operator-selected replacement. Every
substituted POST response identifies the substitution and its provenance status:

- `canonical-recovered`
- `captured-original`
- `operator-attested`
- `alternate-master`

## Origin and access

Curio has one public HTTP origin for REST, MCP, static media, IPFS, and Arweave.
Internal Kubo and AR.IO addresses are never returned to clients.

Curio is intended for a trusted household or studio network and has no user
authentication. `CURIO_PUBLIC_BASE_URL` can set an explicit origin. Forwarded
headers are accepted only from an immediate proxy listed in
`CURIO_TRUSTED_PROXY_CIDRS`.

## Wallet discovery

Curio reads Ethereum mainnet inventory through Blockscout and BENS, and Tezos
mainnet inventory through TzKT. Wallet results are discovery data, not proof of
authorship or authenticity. Curio does not currently resolve a contract/token
pair by querying a chain RPC.

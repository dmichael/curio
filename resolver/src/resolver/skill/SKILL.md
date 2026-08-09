---
name: curio
description: Store and play NFT media through a trusted-network Curio appliance.
---

# Curio

Curio stores media references and local files, then serves them from one origin.
Use `GET /openapi.json` for the complete REST schema.

## Store one work

Submit a reference:

```text
POST /resolve?ref=<reference>
```

A JSON body is also accepted:

```json
{"ref":"ipfs://bafy.../artwork"}
```

Submit a local file as multipart:

```text
POST /resolve                  field: file
```

A successful response has `status` equal to `ready` or `live-dependent` and
contains `media_url`. Status `failed` means Curio did not register the
reference. Curio accepts IPFS, Arweave, HTTP, `data:` metadata or media, and
Verse artwork pages.

## Play

Give `media_url` directly to a renderer. It is a GET URL of this form:

```text
GET /resolve?ref=<stored-reference>
```

Curio redirects known references to `/ipfs/...`, `/arweave/...`, or
`/media/...`. Unknown references return 404; GET never submits new media.

`playback_method` is `play` for static media and `send` for HTML. A
`live-dependent` HTML result still relies on network resources Curio has not
captured.

## Store a wallet or contract

```text
POST /seed?ref=<wallet-or-contract>
GET  /seed/<job-id>
```

Use `scope=held|published|created|contract`. `published` and `created` are
Tezos-only. `contract` accepts a literal Ethereum or Tezos token-contract
address. Fully burned authored works are omitted unless `include_burned=true`.

Use `GET /wallet?ref=...` to browse discovery data before starting a seed job.
Wallet data is not proof of authorship or authenticity.

## Curation

```text
GET/POST/DELETE /favorites
GET/POST/DELETE /override
GET             /library
```

Favorites organize selected references. Overrides map dead references to
operator-selected replacements and always disclose their provenance status:
`canonical-recovered`, `captured-original`, `operator-attested`, or
`alternate-master`.

Curio has no user authentication. It is intended for a trusted household or
studio network and should not be exposed directly to the public internet.

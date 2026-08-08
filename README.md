# Curio

Curio resolves media references, hosts the resulting artifact locally, and
returns a URL on the origin the client contacted.

## Install

A Linux user with access to Docker and its Compose plugin can install without
privileged host mutation:

```sh
curl -fsSL https://github.com/dmichael/curio/releases/latest/download/install.sh | sh
```

Application files use `$XDG_DATA_HOME/curio/app`, configuration uses
`$XDG_CONFIG_HOME/curio/curio.env`, and state uses
`$XDG_DATA_HOME/curio/state` by default. The installer creates a curator token;
mutating requests send it as `Authorization: Bearer <token>`.

```sh
curio version
curio update --check
curio update
curio update --version vX.Y.Z
```

Updates are operator initiated. Release archives are checksum verified and a
requested release tag must match the package version in the archive.

## Serving model

- IPFS references are served through Curio's `/ipfs/<cid>/<path>` route, which
  proxies Kubo without changing the CID or DAG.
- Arweave references are served through `/arweave/<transaction>/<path>`, which
  proxies AR.IO without dropping manifest paths.
- HTTP, inline data, and uploads use `/media/<id>` in Curio's static store.
  They are never implicitly added to IPFS.

`GET /resolve?ref=…` returns `media_url`, `source_kind`, `keep_state`, and a
SHA-256 integrity record for static objects. `POST /keep?ref=…` promotes static
objects and requests an IPFS pin. AR.IO r81 has no documented selected-data
retention control, so Curio reports Arweave keep as unsupported rather than
claiming its evictable cache is durable.

The resolver, static media, and both gateway routes share one external origin.
Internal service ports never appear in resolution responses. When behind a
trusted proxy, set `CURIO_TRUSTED_PROXY_HEADERS=true`; otherwise URLs are
constructed from the request origin.

## Security

Read-only resolution and media serving can be public. Keep, upload, seed,
favorites, overrides, and replacement operations require the curator token.
Source fetching rejects literal private targets and remains bounded; deploy a
network policy appropriate to public use.

not built: runtime HTML dependency capture (needs a replay/package capture implementation)
not built: Arweave durable selected-data retention (needs an AR.IO API or supported r81 retention mechanism)

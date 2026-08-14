<h1>Curio</h1>


<h3>A preservation and delivery appliance for digital art.</h3>
<a href="https://verse.works/items/ethereum/0xf7d3e687883b98eafb8808fa9b53ee065fb2e43f/1"><img src="docs/flatiron.jpg" align="right" width="475" alt="Flatiron — a painting by David Michael"></a>

<p>Curio solves the gap between having a reference to a digital artwork and being
able to display that artwork reliably in the future.</p>
<p>Digital art is often scattered across IPFS, Arweave, ordinary web servers,
metadata documents, and local files. These sources can be difficult for media
players to resolve, dependent on third-party infrastructure, or vulnerable to
link rot. Curio resolves each reference to its final media, stores it locally,
and makes it reliably available to players through one URL space on your own
server. It uses Kubo for IPFS, AR.IO Core for Arweave, and a local static store
for ordinary files.</p>

<br clear="left">



## Install

Curio needs Linux, Docker Engine, and the Docker Compose plugin. Install the
latest release as the user who will run it:

```bash
curl -fsSL https://github.com/dmichael/curio/releases/latest/download/install.sh | sh
```

The bootstrap downloads an immutable release archive and verifies its SHA-256
checksum before installing it. Installation uses per-user XDG paths and does
not call `sudo`.

```bash
curio status
curio health
curio logs resolver --follow
```

Configuration defaults to `~/.config/curio/curio.env`; state defaults to
`~/.local/share/curio/state`.

## Store a reference

`POST /resolve` expresses storage intent:

```bash
curl -X POST --get 'http://localhost:8090/resolve' \
  --data-urlencode 'ref=ipfs://bafy.../artwork'
```

JSON input is also accepted:

```bash
curl -X POST 'http://localhost:8090/resolve' \
  -H 'Content-Type: application/json' \
  -d '{"ref":"ar://transaction-id"}'
```

A successful response has status `ready` or `live-dependent` and includes a
`media_url`. Players fetch that URL with GET; Curio redirects it to the stored
source-native media path. A reference that has not been submitted successfully
returns 404 from GET `/resolve`.

Curio understands:

| Input | Storage |
|---|---|
| IPFS URI, path, or gateway URL | Pinned in Kubo and served at `/ipfs/...` |
| Arweave transaction or manifest path | Fetched through AR.IO Core and served at `/arweave/...` |
| HTTP media | Stored locally by SHA-256 and served at `/media/...` |
| HTTP or inline JSON metadata | Followed to its selected media reference |
| Other `data:` media | Decoded into the static store |
| Small UnixFS wrappers | Followed to the selected media |
| Verse artwork pages and /items/ URLs | Chain-first: on-chain tokenURI resolved recursively; scrape fallback only when chain resolution is impossible |

HTML works can depend on uncaptured scripts, APIs, or other resources. Curio
stores the primary artifact but reports these results as `live-dependent`.

## Store a file

The same endpoint accepts an upload:

```bash
curl -X POST -F 'file=@master.mp4' 'http://localhost:8090/resolve'
```

Uploads remain in Curio's static store. Curio does not add them to IPFS.

## Wallets

Curio uses Blockscout and BENS to discover Ethereum mainnet holdings and token
coordinates, then reads each token's current `tokenURI` or `uri` through
Ethereum RPC. Blockscout's cached metadata is only a disclosed fallback. Tezos
mainnet inventory comes from TzKT:

```bash
curl --get 'http://localhost:8090/wallet' \
  --data-urlencode 'ref=name.eth'
```

`POST /seed` starts a background storage job for a wallet or contract; poll
`/seed/<job-id>`. Ethereum creator lookup is not implemented.

## Connect your agent

Curio serves MCP at `http://<host>:8090/mcp` (streamable HTTP); the tool
descriptions carry the same semantics as the REST routes below. Register it
with Claude Code:

```bash
claude mcp add --transport http curio http://<host>:8090/mcp
```

Any other MCP-capable tool — Codex, OpenCode, Gemini, Cursor, and the rest —
registers the same URL through its own config. REST callers use the schema
at `/openapi.json` instead.

Curio can also export catalogued works as an unsigned DP-1 playlist for
DP-1 players such as the Feral File FF1. Install Feral File's official
`@feralfile/cli` (`ff-cli`) locally for signing and playback; see
[docs/dp1-players.md](docs/dp1-players.md) for operation and orientation checks.

## API and services

OpenAPI is available at `/docs` and `/openapi.json`. MCP is mounted at `/mcp`.
The main routes are `/resolve`, `/wallet`, `/seed`, `/favorites`, `/override`,
`/library`, and `/healthz`.

The appliance runs three local services:

- Curio resolver
- Kubo
- AR.IO Core

### External dependencies

Curio is self-hosted, but source discovery and retrieval necessarily contact
external networks. The default dependencies and their trust boundaries are:

| Dependency | Used for | Not trusted for |
|---|---|---|
| Ethereum JSON-RPC (`ethereum.publicnode.com` by default) | Current ERC-721 `tokenURI` and ERC-1155 `uri` reads | Wallet enumeration or authorship |
| Blockscout (`eth.blockscout.com`) | Ethereum holding and contract/token discovery | Current token metadata or media identity |
| BENS (`bens.services.blockscout.com`) | Resolving `.eth` names for wallet discovery | Token metadata |
| TzKT (`api.tzkt.io`) | Tezos names, balances, contracts, creator indexes, and indexed token metadata | Proof of authorship, authenticity, or ownership history |
| IPFS network | Retrieving content addressed by an IPFS CID | Availability guarantees |
| Public IPFS gateways (`ipfs.io`, `dweb.link`) | Recovery copies when normal IPFS retrieval fails | Canonical identity without CID verification |
| AR.IO trusted gateways (`arweave.net`, `ar-io.dev`, `turbo-gateway.com`, `permagate.io`) | Retrieving Arweave transactions into the local Core | Proof that Curio created an Arweave replica |
| Referenced HTTP origins | Fetching ordinary metadata and media | Permanence or content identity unless the reference supplies a hash |

Installation and updates may also contact GitHub Releases, Docker Hub, and the
GitHub Container Registry; they are not runtime media dependencies. See [the design](docs/design.md) and
[the indexer trust decision](docs/decisions/0002-indexers-discover-chain-defines-ethereum-media.md).

Port 8090 is the only public HTTP port. Kubo also publishes port 4001 over TCP
and UDP for IPFS peers. Kubo's HTTP interfaces and AR.IO Core stay on the
private Compose network.

Curio is designed for a trusted household or studio network and has no user
authentication. Do not expose it directly to the public internet. Returned URLs
normally use the request origin. Reverse-proxy deployments can set
`CURIO_PUBLIC_BASE_URL` or allowlist the immediate proxy with
`CURIO_TRUSTED_PROXY_CIDRS`.

Back up `curio.env` and the state directory. See [the design](docs/design.md),
[appliance notes](docs/appliance.md), [testing guide](docs/appliance-testing.md),
and [security policy](SECURITY.md).

## Development

```bash
cd resolver
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
cd ..
./appliance/tests/test-appliance.sh
```

Curio is available under the [MIT License](LICENSE). Third-party components
retain their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

<h1 align="center">Curio</h1>

<p align="center"> <strong>Keep the media behind your NFTs available.</strong> </p>

<p align="center"> <a href="#install">Install</a> &bull; <a href="#resolution-coverage">Resolution</a> &bull; <a href="#chain-coverage">Chains</a> &bull; <a href="docs/design.md">Design</a> &bull; <a href="SECURITY.md">Security</a> </p>

<p align="center"> <a href="https://github.com/dmichael/curio/actions/workflows/ci.yml"> <img src="https://github.com/dmichael/curio/actions/workflows/ci.yml/badge.svg" alt="CI" /> </a> <a href="LICENSE"> <img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License" /> </a> </p>

Curio finds the media behind your NFTs and gives players on your network one place to ask for it. Give Curio a wallet, metadata document, or media reference and it finds the image, video, audio, or HTML work behind it.

Use Curio to keep selected works available and to repair dead media references with explicit, provenance-labelled replacements. Browsing does not add anything to the library.

## Install

You need a Linux host with Docker Engine and the Docker Compose plugin. Give the host a stable LAN address, preferably with a DHCP reservation.

```bash
# Optional: uncomment to skip the LAN address prompt.
# export CURIO_LAN_ADDRESS=192.168.1.50

curl -fsSL https://github.com/dmichael/curio/releases/latest/download/install.sh | bash
```

The bootstrap downloads the latest release archive, verifies its published SHA-256 checksum, and asks for `sudo` only when the system install begins. Set `CURIO_VERSION=vX.Y.Z` before running it to install a specific release.

Configuration is kept in `/etc/curio/curio.env` and state in `/var/lib/curio`. The installer is safe to rerun after an update or interrupted install. It does not install Docker or change the host firewall.

```bash
curio status
curio health
curio logs resolver --follow
```

## Resolution coverage

In Curio, **resolution** means following a reference until it reaches the actual media, then returning a playable URL. Curio stays out of the media path; the player fetches the bytes from the IPFS gateway, Arweave gateway, or original HTTP source.

| Input | What Curio does |
|---|---|
| `ipfs://CID/path` | Returns the same content through the local IPFS gateway |
| `/ipfs/CID` and IPFS gateway URLs | Extracts the CID and rewrites the URL to the local gateway |
| IPFS JSON metadata | Follows animation or artifact fields, then chooses the largest image candidate |
| UnixFS directory | Lists the directory and resolves its largest media file |
| `ar://txid/path` | Returns the transaction or manifest path through the local Arweave gateway |
| `arweave.net/txid/path` | Rewrites the reference to the local Arweave gateway |
| HTTP(S) media URL | Returns the source URL with playback and content-type hints |
| HTTP(S) JSON metadata | Reads the metadata and follows its media reference |
| `data:application/json,...` | Decodes on-chain metadata and resolves its media |
| other `data:` media | Returns the self-contained URI directly |
| `verse.works/artworks/...` | Follows the page's token URI, interactive iframe, or original artwork image |

Extensionless IPFS and HTTP references are probed for content type. HTML works use the `send` playback method; static media uses `play`. Operator overrides are checked at every metadata recursion step, so a dead nested media reference can be replaced without changing the token metadata.

Curio does not currently accept a chain contract/token pair as a `/resolve` input or call a chain RPC to discover its token URI.

## Chain coverage

Wallet and contract discovery currently covers two mainnets:

| Chain | Sources | Supported inventory |
|---|---|---|
| Ethereum mainnet | Blockscout and BENS | ERC-721/ERC-1155 holdings, ENS names, and contract-wide token listings |
| Tezos mainnet | TzKT | FA2 holdings, `.tez` names, first-minted works, creator-attributed works, and contract-wide listings |

Ethereum creator or published-catalog lookup is not implemented because Curio does not have a reliable keyless authorship index for it. Curio also does not yet resolve a contract/token pair by querying a chain RPC directly; wallet inventory uses metadata returned by the public indexers above.

IPFS, Arweave, HTTP, and on-chain `data:` media references are otherwise chain-independent. Wallet discovery for other EVM networks, Solana, and other chains is not currently supported.

## Use

Find the media behind a reference:

```bash
curl --get 'http://192.168.1.50:8090/resolve' \
  --data-urlencode 'ref=ipfs://bafy.../artwork'
```

Find and keep one work:

```bash
curl --get 'http://192.168.1.50:8090/resolve' \
  --data-urlencode 'ref=ar://transaction-id' \
  --data-urlencode 'pin=1'
```

Inspect and seed a wallet:

```bash
curl --get 'http://192.168.1.50:8090/wallet' \
  --data-urlencode 'ref=name.eth'

curl -X POST --get 'http://192.168.1.50:8090/seed' \
  --data-urlencode 'ref=name.eth'
```

Upload an operator-supplied file:

```bash
curl -F 'file=@master.mp4' 'http://192.168.1.50:8090/store'
```

On success, Curio returns the file's local CID and checksum. Curio will not use it as a replacement until the operator adds an override.

OpenAPI documentation is available from a running resolver at `/docs`. The main endpoints are `/resolve`, `/wallet`, `/seed`, `/store`, `/override`, `/favorites`, `/library`, and `/healthz`; streamable HTTP MCP is mounted at `/mcp`.

## State and provenance

IPFS pins are the durable content library. Arweave payloads are cached and may be evicted, so Curio keeps a ledger of content it deliberately warmed.

Overrides for dead references use one of four provenance statuses: `canonical-recovered`, `captured-original`, `operator-attested`, or `alternate-master`. Every substitution is disclosed in the resolver response.

Back up both `/etc/curio` and `/var/lib/curio`. The IPFS store may contain unique uploaded or captured files, not only reproducible cache data.

## Technical details

The appliance publishes three services on the LAN:

| Port | Service | Purpose |
|---|---|---|
| `3000` | Arweave gateway (AR.IO) | Fetch and cache Arweave content |
| `8080` | IPFS gateway (Kubo) | Fetch and serve IPFS content |
| `8090` | Curio resolver | REST, OpenAPI, skills, and MCP |

The IPFS admin API and the Arweave gateway's internal services stay on the private Compose network.

## Security

Curio 0.1 assumes a trusted LAN and does not include authentication or TLS. Keep ports 3000, 8080, and 8090 behind your firewall and do not forward them from an internet-facing router. See [SECURITY.md](SECURITY.md) for the full deployment boundary and vulnerability-reporting process.

## Development

The appliance installer is the supported deployment path. For resolver work:

```bash
cd resolver
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
```

The resolver requires Python 3.11 or newer. Settings use the `RESOLVER_` environment variable prefix; defaults are in [`resolver/src/resolver/config.py`](resolver/src/resolver/config.py).

## Documentation

- [Design and trust model](docs/design.md)
- [Appliance specification](docs/appliance.md)
- [Disposable-VM testing](docs/appliance-testing.md)
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)

## License

Curio's source is available under the [MIT License](LICENSE). The appliance pulls third-party components under their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

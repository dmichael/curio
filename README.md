<h1 align="center">Curio</h1>

<p align="center"><strong>Keep the media behind your NFTs usable.</strong></p>

Curio follows NFT media references and returns playable URLs on your own
server. It uses Kubo for IPFS, AR.IO for Arweave, and a local static store for
ordinary files.

## Install

Curio needs Linux, Docker Engine, and the Docker Compose plugin. Install from a
checkout as the user who will run it:

```bash
git clone https://github.com/dmichael/curio.git
cd curio
./appliance/install.sh
```

The installer uses per-user XDG paths and does not call `sudo`. No release
assets have been published yet, so the release download URL and remote update
commands are not available.

```bash
curio status
curio health
curio logs resolver --follow
```

Configuration defaults to `~/.config/curio/curio.env`; state defaults to
`~/.local/share/curio/state`.

## Resolve media

```bash
curl --get 'http://localhost:8090/resolve' \
  --data-urlencode 'ref=ipfs://bafy.../artwork'
```

A successful response includes `media_url` on the Curio origin. Curio supports:

| Input | Result |
|---|---|
| IPFS URI, path, or gateway URL | Served through Kubo at `/ipfs/...` |
| Arweave transaction or manifest path | Served through AR.IO at `/arweave/...` |
| HTTP media | Copied into Curio's static cache and served at `/media/...` |
| HTTP or inline JSON metadata | Followed to its media reference |
| Other `data:` media | Decoded into the static cache |
| Small UnixFS wrappers and Verse artwork pages | Followed to the selected media |

HTML works can depend on uncaptured scripts, APIs, or other resources. Curio
marks these results `live-dependent`.

## Keep media

Mutating requests need the curator token from `curio.env`:

```bash
curl -X POST -H 'Authorization: Bearer YOUR_TOKEN' --get \
  'http://localhost:8090/keep' \
  --data-urlencode 'ref=ar://transaction-id'
```

Keep means:

- IPFS: pin the CID root in Kubo.
- Arweave: fully fetch the work and verify it in the same persistent AR.IO
  Core used for playback.
- HTTP or inline media: mark the local static object as kept.

AR.IO Core does not automatically delete fetched content. Arweave keep is an
eager download check, not a second storage tier or a new Arweave replica.

Upload a local file with:

```bash
curl -X POST -H 'Authorization: Bearer YOUR_TOKEN' \
  -F 'file=@master.mp4' 'http://localhost:8090/store'
```

Uploads remain in Curio's static store. Curio does not add them to IPFS.

## Wallets

Curio can list Ethereum mainnet NFTs through Blockscout and BENS, and Tezos
mainnet NFTs through TzKT:

```bash
curl --get 'http://localhost:8090/wallet' \
  --data-urlencode 'ref=name.eth'
```

Start a background keep job for a wallet or contract with authenticated
`POST /seed`, then poll `/seed/<job-id>`. Ethereum creator lookup and direct
contract/token RPC resolution are not implemented.

## API and services

OpenAPI is available at `/docs` and `/openapi.json`. MCP is mounted at `/mcp`.
The main routes are `/resolve`, `/wallet`, `/keep`, `/seed`, `/store`,
`/favorites`, `/override`, `/library`, and `/healthz`.

The appliance runs three services:

- Curio resolver
- Kubo
- AR.IO Core

Port 8090 is the only public HTTP port. Kubo also publishes port 4001 over TCP
and UDP for IPFS peers. Kubo's HTTP interfaces and AR.IO Core stay on the
private Compose network.

Returned URLs normally use the request origin. Reverse-proxy deployments can
set `CURIO_PUBLIC_BASE_URL` or allowlist the immediate proxy with
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

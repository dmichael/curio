<h1 align="center">Curio</h1>

<p align="center"><strong>Keep the media behind your NFTs usable.</strong></p>

<p align="center"><a href="#install">Install</a> &bull; <a href="#resolution-coverage">Resolution</a> &bull; <a href="#chain-coverage">Chains</a> &bull; <a href="docs/design.md">Design</a> &bull; <a href="SECURITY.md">Security</a></p>

Curio resolves a wallet, metadata document, or media reference into media served
from one Curio origin. It follows the reference, uses the backend appropriate to
the final work, and returns a Curio URL rather than an upstream gateway URL.

## Install

Curio is a per-user Linux appliance. It needs Docker Engine and the Docker
Compose plugin available to that user; it does not use `sudo`, configure a LAN
address, install Docker, or change firewall rules.

There is not yet a published Curio release asset at the GitHub release URL.
Until release assets exist, install this checkout as the intended user:

```bash
git clone https://github.com/dmichael/curio.git
cd curio
./appliance/install.sh
```

The source installer creates configuration at
`$XDG_CONFIG_HOME/curio/curio.env` (default `~/.config/curio/curio.env`),
immutable application copies below `$XDG_DATA_HOME/curio/app/releases`, and
state below `$XDG_DATA_HOME/curio/state`. `CURIO_APP_ROOT` and
`CURIO_DATA_ROOT` can select other absolute, non-root locations before the
first install.

The future release bootstrap is
`https://github.com/dmichael/curio/releases/latest/download/install.sh`. It
verifies `curio-appliance.tar.gz.sha256` before running the archived installer,
but cannot succeed until a release publishes those assets. Do not treat that
URL as an available installer today.

```bash
curio status
curio health
curio logs resolver --follow
curio version
curio update --check
curio update
curio update --version vX.Y.Z
```

`curio update` is operator-invoked and the installer rolls back `current` when
the replacement graph fails health. The checked-in/source installer does not
yet fetch or select verified release artifacts; `update --check` depends on a
published `VERSION` file and `update --version` is not a release selector yet.
Use a verified release bootstrap only once release assets are published.

## Resolution coverage

A successful `/resolve` response includes `media_url` (also exposed as the
legacy `resolved_url`) on the Curio origin that handled the request. Curio
proxies native paths through that origin and serves ordinary bytes from its own
static store.

| Input | Resolution and serving |
|---|---|
| `ipfs://CID/path`, `/ipfs/CID`, or gateway URL | Keeps the CID/path identity and serves `/ipfs/CID/path` through Kubo |
| IPFS JSON metadata or UnixFS directory | Follows media fields or a small directory wrapper to the final IPFS artifact |
| `ar://txid/path` or `arweave.net/txid/path` | Keeps the transaction/manifest path and serves `/arweave/txid/path` through AR.IO |
| HTTP(S) media | Fetches a bounded copy and serves `/media/<id>` from Curio static storage |
| HTTP(S) JSON or `data:application/json` | Follows metadata to its final media artifact |
| Other `data:` media | Decodes it into Curio static storage and serves `/media/<id>` |
| `verse.works/artworks/...` | Follows token URI, iframe, or artwork image |

Resolution can populate an evictable cache. It does not keep a work. HTML
runtime responses are marked `live-dependent`: saving the HTML shell does not
preserve scripts, APIs, workers, or origin behavior.

## Keep and access control

Keep is explicit and source-appropriate:

- IPFS keep pins the canonical DAG in Kubo; Kubo then seeds it.
- Arweave keep hydrates and verifies the private retained r81 Core plane. This
  is not an AR.IO r81 pin API or a claim of new Arweave replication.
- HTTP, `data:`, and uploads remain in Curio static storage; none enters IPFS
  unless a future explicit publication operation says so.

Use `POST /keep?ref=...` for synchronous keep, or `GET /resolve?ref=...&pin=1`
for the existing convenience action. For IPFS that convenience action returns
`keep_state: "pending"` and `pin_scheduled: true`; it is not completion
proof. Arweave and static results report their completed or failed promotion.
Wallet-wide retention is `POST /seed?ref=...`; it runs as a background job.

Read-only resolver, media, library, health, and skill routes may be exposed
publicly. Every mutation requires `Authorization: Bearer <CURIO_CURATOR_TOKEN>`:
keep/pin, seed, upload, favorite changes, and override changes. The installer
generates the token in `curio.env`.

Examples (replace the origin and token):

```bash
curl --get 'https://curio.example/resolve' \
  --data-urlencode 'ref=ipfs://bafy.../artwork'

curl -X POST -H 'Authorization: Bearer YOUR_TOKEN' --get \
  'https://curio.example/keep' \
  --data-urlencode 'ref=ar://transaction-id'

curl -X POST -H 'Authorization: Bearer YOUR_TOKEN' \
  -F 'file=@master.mp4' 'https://curio.example/store'
```

OpenAPI is at `/openapi.json` and `/docs`; streamable HTTP MCP is at `/mcp`.
The shipped agent instructions are at `/skill` and `/skill/nft-preservation`.

## Chain coverage

Wallet and contract discovery covers two mainnets:

| Chain | Sources | Supported inventory |
|---|---|---|
| Ethereum mainnet | Blockscout and BENS | ERC-721/ERC-1155 holdings, ENS names, and contract-wide listings |
| Tezos mainnet | TzKT | FA2 holdings, `.tez` names, first-minted works, creator-attributed works, and contract-wide listings |

Ethereum has no reliable keyless creator-attribution index. Curio also does not
resolve a contract/token pair by querying a chain RPC directly. Other EVM
networks, Solana, and other chains are not supported for wallet discovery.

## Network and provenance

Only port `8090` is Curio's public HTTP origin for REST, MCP, media, IPFS, and
Arweave paths. Kubo and both ordinary and retained AR.IO native planes remain
on the Compose network. Kubo additionally publishes swarm `4001/tcp` and
`4001/udp` for participation.

Direct HTTP requests derive returned URLs from the request origin. A reverse
proxy must preserve the external Host/scheme or set `CURIO_PUBLIC_BASE_URL`.
Forwarded headers are **not currently consumed**, so there is no trusted-proxy
header opt-in at this revision; this remains a documented implementation gap.

`/healthz` reports backend reachability and participation evidence. Kubo and
AR.IO are enabled by default, but neither a running daemon nor advertised
addresses prove public reachability; AR.IO r81 currently reports that evidence
as unknown.

Back up the actual XDG configuration and state paths. State includes Kubo pins,
the static media store, operator records, and separate ordinary and retained
AR.IO trees. Cache is not preservation; retained records and static/IPFS kept
content must be backed up if their upstreams matter.

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
git diff --check
```

See [docs/design.md](docs/design.md), [docs/appliance.md](docs/appliance.md),
[docs/appliance-testing.md](docs/appliance-testing.md), and
[SECURITY.md](SECURITY.md).

## License

Curio is available under the [MIT License](LICENSE). Third-party appliance
components retain their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

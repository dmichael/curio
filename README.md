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

## Connect your agent

Curio is agent-driven. Its first-class [Model Context
Protocol](https://modelcontextprotocol.io/) (MCP) server is available at
`http://<host>:8090/mcp` using streamable HTTP. It lets an agent browse wallet
inventories, resolve and look up works, seed a collection, manage favorites and
provenance overrides, inspect library health, and produce DP-1 playlists.
Binary file uploads remain REST-only (`multipart POST /resolve`).

Register it with Claude Code:

```bash
claude mcp add --transport http curio http://<host>:8090/mcp
```

Any other MCP-capable tool — Codex, OpenCode, Gemini, Cursor, and the rest —
registers the same streamable-HTTP URL through its own configuration. The MCP
tool surface is `resolve`, `lookup`, `wallet_tokens`, `seed_wallet`,
`seed_status`, `list_overrides`, `add_override`, `remove_override`,
`list_favorites`, `add_favorite`, `remove_favorite`, `dp1_playlist`, and
`library_status`. REST callers can use the OpenAPI schema at `/openapi.json`
instead.

## Open Curio in a browser

The installed appliance serves Curio's minimal web interface at
`http://<curio-host>:8090/`. Enter an artwork URI and select **Resolve** to
preview it. Preview media may use Curio's evictable caches but is not added to
the durable library. Select **Save to Curio** before resolving to store it with
the same semantics as the REST and MCP APIs. Both paths open the result at
`/display`.

## What your agent can do

Give Curio an artwork reference, a wallet, or a local file. It follows metadata
to final media and keeps the result on the appliance:

| Source | Curio retains it as |
|---|---|
| IPFS URI, path, or gateway URL | A recursive Kubo pin, served from Curio |
| Arweave transaction or manifest path | Content warmed in the appliance's AR.IO Core, served from Curio |
| HTTP or `data:` media, metadata, or an uploaded file | A SHA-256-addressed local object |
| Verse artwork page or `/items/` URL | On-chain token metadata first; page scraping only as a fallback |

It can inventory Ethereum and Tezos wallets, seed a collection in the
background, record favorites and disclosed replacement provenance, and report
what the appliance holds. Runtime HTML is retained as its primary artifact but
is marked `live-dependent` when it still needs uncaptured network resources.

Curio also emits unsigned [DP-1](https://github.com/display-protocol/dp1)
playlists for catalogued works. Use the display operator's tooling to sign and
deliver them; see [DP-1 player operation](docs/dp1-players.md).

## REST and appliance details

MCP is the normal control surface. REST exists for integrations, browser use,
and multipart file uploads; its complete schema is served by the appliance at
`/docs` and `/openapi.json`.

Curio runs the resolver, Kubo, and AR.IO Core. Port 8090 is its public HTTP
origin; Kubo's peer port 4001 is also published for IPFS traffic. Curio has no
user authentication and belongs on a trusted household or studio network, not
the public internet. Back up `curio.env` and the state directory.

For architecture, trust boundaries, appliance operations, and security, see
[the design](docs/design.md), [appliance notes](docs/appliance.md), and the
[security policy](SECURITY.md).

Curio is available under the [MIT License](LICENSE). Third-party components
retain their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

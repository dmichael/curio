# ff1-content-sidecar

A single always-on LAN box that specializes in **serving URI-addressable media**,
and doubles as a **universal gateway and data source for content-addressed media
(IPFS + Arweave)**. The Feral File FF1 art computer is its first consumer, not its
only one — any renderer on the LAN can use it.

_(Working name. See `docs/design.md` for the full rationale, posture, and
trust model.)_

## The three planes

| Plane | What | Where |
|---|---|---|
| IPFS gateway | content-addressed fetch + cache (Kubo) | on the box, `:8080` |
| Arweave gateway | txid fetch + cache (ar-io-node) | on the box, `:3000` |
| **Resolver** | turns any reference into a playable box-local URL | this repo, `resolver/` |

The gateways are stock and installed separately (see `deploy/`). The resolver is
the component this project exists to build: it absorbs the reference-interpretation
that today lives client-side in the `ff1` repo, points every fetch at the box's own
gateways instead of a public one, and hands consumers one stable origin.

## Layout

- `resolver/` — the Python resolver service (FastAPI + httpx).
- `deploy/` — Ansible for a repeatable install onto a small always-on SBC/server.
- `docs/design.md` — architecture, posture, build order, open decisions.

## Runbook

```bash
# Resolver, locally
cd resolver && pip install -e ".[dev]" && content-resolver   # serves :8090

# Deploy to the sidecar host
cd deploy
cp inventory.example.ini inventory.ini   # then point it at your box
ansible-playbook site.yml
```

Resolve anything (use the box's LAN IP — renderers like the FF1 don't
resolve mDNS `.local` names):

```bash
curl 'http://<sidecar-ip>:8090/resolve?ref=ipfs://bafy.../art'
curl 'http://<sidecar-ip>:8090/healthz'
```

Seed the caches from a wallet (pins every IPFS ref the wallet's NFTs carry,
warms the Arweave cache, captures unhashed HTTP media with provenance;
ETH + Tezos, addresses or names):

```bash
curl -X POST 'http://<sidecar-ip>:8090/seed?ref=name.eth'   # or 0x…, tz1…, name.tez
curl 'http://<sidecar-ip>:8090/seed/<job-id>'               # poll progress
```

Works whose canonical media is dead resolve through the operator's override
registry (`deploy/overrides.example.toml` documents the format; substitutions
are always disclosed in the response). Repair one end to end:

```bash
# Store a local master file on the box (pinned, provenance recorded)
curl -F file=@master.mp4 'http://<sidecar-ip>:8090/store'
# Point the dead ref at it (disclosed substitution), then verify via /resolve
curl -X POST 'http://<sidecar-ip>:8090/override' -H 'content-type: application/json' \
  -d '{"ref":"ipfs://QmDEAD…","replacement":"ipfs://bafy…","status":"alternate-master"}'
curl 'http://<sidecar-ip>:8090/override?raw=1' > deploy/overrides.toml   # snapshot back
```

See `docs/design.md` § Dead works.

Agents can use the box without being told anything but its address: the API is
self-documented at `/skill` (agent instructions, served by the service itself),
`/docs` + `/openapi.json` (schema), and `/mcp` (the same capabilities as MCP
tools over streamable HTTP).

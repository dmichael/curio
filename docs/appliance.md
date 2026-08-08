# Curio appliance specification

Status: implemented packaging and media-plane behavior at 0.2.0.

## Deployment contract

Curio is a no-sudo, per-user Linux appliance. Docker Engine and `docker
compose` must already be usable by the installing user. The installer does not
install Docker, alter daemon settings, firewall rules, routers, DNS, or
unrelated containers.

By default it stores:

```text
$XDG_CONFIG_HOME/curio/curio.env       configuration and curator token
$XDG_DATA_HOME/curio/app/releases/     immutable installed application copies
$XDG_DATA_HOME/curio/app/current        atomic symlink to the active release
$XDG_DATA_HOME/curio/state/             persistent media and service state
$XDG_BIN_HOME/curio                     operator wrapper
```

`CURIO_APP_ROOT` and `CURIO_DATA_ROOT` may choose safe absolute non-root paths
on first install. The installer creates configuration with mode 0600 and runs
state-writing containers as the installing UID:GID.

## Compose graph

The complete graph has exactly three services:

1. `resolver` — FastAPI REST, MCP, skills, static media, and same-origin native
   gateway proxy.
2. `kubo` — IPFS gateway/API and pin store.
3. `ar-io-core` — one pinned AR.IO Core with persistent `/app/data` state.

Core uses embedded LMDB (`CHAIN_CACHE_TYPE=lmdb`), directly trusts
`https://arweave.net`, and has direct public trusted gateway URLs plus an
on-demand retrieval order that does not need Envoy. Observer work and ANS-104
unbundling/indexing are disabled. `ENABLE_CHUNK_DATA_CACHE_CLEANUP=false` and
there is no contiguous-cache cleanup threshold, so Curio does not automatically
delete cached content. Existing historical state directories and obsolete
configuration keys are left untouched by upgrades.

On first install the installer obtains `START_HEIGHT` with a small Node command
inside the already pinned Core image; it does not require a host Node, jq,
Redis, Envoy, or EDS files.

## Network surface

There is one public HTTP origin:

| Host port | Service | Purpose |
|---|---|---|
| `8090/tcp` (or `CURIO_PORT`) | resolver | REST, OpenAPI, MCP, skills, `/media`, `/ipfs`, and `/arweave` |

Kubo additionally publishes native swarm participation on `4001/tcp` and
`4001/udp`. Kubo gateway/API and Core have no host binding. Consumers use
`http(s)://<curio-origin>:8090/ipfs/...` and `/arweave/...`, never internal
ports.

## Arweave behavior

All `/arweave` reads route to the one Core. Resolver metadata reads, playback,
and ordinary resolution can populate its persistent cache. An explicit keep
fully fetches the exact transaction/path and then fully reads it again,
requiring a native `X-Cache: HIT`. This is eager fetch/verification on the same
Core, not movement between tiers and not a claim that Curio replicated data into
the Arweave network. AR.IO has no Curio pin API.

## Resolver and configuration

Compose supplies:

```text
RESOLVER_IPFS_INTERNAL=http://kubo:8080
RESOLVER_IPFS_API=http://kubo:5001
RESOLVER_ARWEAVE_INTERNAL=http://ar-io-core:4000
RESOLVER_ARWEAVE_COLD_TIMEOUT=300
RESOLVER_STATIC_ROOT=/state/media
RESOLVER_STATIC_CACHE_MAX_BYTES=1000000000
```

The generated `curio.env` contains roots, host UID/GID, curator token, port,
cache limits, and public/trusted-proxy origin settings. HTTP, inline data, and
uploads use Curio static storage and never reach IPFS implicitly. IPFS keeps
pin the canonical CID root. HTML runtime responses remain `live-dependent`.

## Installer and state

The installer stages a release, atomically switches `current`, builds the
resolver, and starts Compose with `--wait --remove-orphans`. Thus upgrades
remove obsolete Compose containers. If startup fails, it stops the failed graph,
restores the prior symlink, and starts that prior graph, so rollback can restore
its previous services. It never recursively deletes state, runs Docker pruning,
IPFS garbage collection, or pin removal.

Back up `curio.env` and state, especially:

```text
state/ipfs/
state/ar-io/                    persistent Core state and first-install height
state/media/                    static objects and SQLite catalogue
state/overrides.toml
state/favorites.json
```

## Acceptance checks

A qualified deployment demonstrates all three services become healthy, only
resolver `8090` and Kubo swarm `4001/tcp,4001/udp` publish host ports, first
Arweave fetch followed by native `X-Cache:HIT`, persistence across recreation
and reboot, Core failure/recovery, and installer rollback. See
[appliance testing](appliance-testing.md).

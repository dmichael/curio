# Curio appliance specification

Status: implemented packaging, media-plane behavior, and opt-in trusted-proxy
origin handling at 0.2.0.

## Deployment contract

Curio is a no-sudo, per-user Linux appliance. Docker Engine and `docker
compose` must already be usable by the installing user. The installer does not
install Docker, alter Docker daemon settings, firewall rules, routers, DNS, or
unrelated containers.

By default it stores:

```text
$XDG_CONFIG_HOME/curio/curio.env       configuration and curator token
$XDG_DATA_HOME/curio/app/releases/     immutable installed application copies
$XDG_DATA_HOME/curio/app/current        atomic symlink to the active release
$XDG_DATA_HOME/curio/state/             all persistent media and service state
$XDG_BIN_HOME/curio                     operator wrapper
```

The usual XDG defaults are `~/.config`, `~/.local/share`, and `~/.local/bin`.
`CURIO_APP_ROOT` and `CURIO_DATA_ROOT` may choose safe absolute non-root paths
on first install. The installer creates the configuration with mode 0600 and
runs state-writing containers as the installing UID:GID.

Install a checkout with:

```bash
./appliance/install.sh
```

The root `install.sh` is a release bootstrap: it downloads
`curio-appliance.tar.gz` and its `.sha256`, verifies the archive, and runs the
archived installer. No release assets are currently published, so the GitHub
release URL is not yet an install path. The bootstrap is the intended verified
release mechanism once assets exist.

## Compose graph

The complete graph has eight services:

1. `resolver` — FastAPI REST, MCP, skills, static media, and same-origin
   native gateway proxy.
2. `kubo` — IPFS gateway/API and pin store.
3. `ar-io-redis` — ordinary r81 Core cache dependency.
4. `ar-io-core` — ordinary evictable r81 Core data plane.
5. `ar-io-retained-redis` — retained Core's separate Redis dependency.
6. `ar-io-retained` — private r81 Core used only for explicit keep intent.
7. `ar-io-observer` — intentionally inert compatibility service required by
   the pinned Envoy DNS configuration.
8. `ar-io-envoy` — ordinary Core's trusted network/upstream and participation
   component.

All images are version and digest pinned. The ordinary Core prefers trusted
AR.IO gateways before explicit untrusted fallback gateways, uses IPv4-first DNS,
and has observer, ANS-104 unbundling, and indexing disabled. On first install
the installer obtains a chain height through the pinned Envoy image and stores
`START_HEIGHT`; the retained Core receives the same initial height once while
keeping separate Core/SQLite state.

The retained Core's only trusted upstream is ordinary Envoy. It has no
contiguous-cache cleanup threshold. Curio records pending/kept/failed
transaction/path intent in its own SQLite registry, fully consumes a retained
Core response, verifies a second native `X-Cache: HIT`, and only then records
`kept`. This is isolated native retained-plane operation, **not** an upstream
AR.IO r81 pin API. A kept transaction/path is served only from the retained
Core; failure is degraded rather than an ordinary-cache fallback.

## Network surface

There is one public HTTP origin:

| Host port | Service | Purpose |
|---|---|---|
| `8090/tcp` (or `CURIO_PORT`) | resolver | REST, OpenAPI, MCP, skills, `/media`, `/ipfs`, and `/arweave` |

Kubo additionally publishes native swarm participation on `4001/tcp` and
`4001/udp`. It is not an HTTP API. Kubo gateway/API, both AR.IO Cores, both
Redis services, and Envoy are Compose-network-only. Consumers use
`http(s)://<curio-origin>:8090/ipfs/...` and `/arweave/...`, never internal
ports.

Kubo and AR.IO are enabled by default. Health reports actual backend state but
does not claim public reachability merely because daemons run. Kubo may expose
advertised addresses without proving inbound reachability; r81 has no equivalent
AR.IO probe, so those participation states can be `unknown`.

## Resolver and configuration

Compose supplies internal addresses and persistent resolver paths:

```text
RESOLVER_IPFS_INTERNAL=http://kubo:8080
RESOLVER_IPFS_API=http://kubo:5001
RESOLVER_ARWEAVE_INTERNAL=http://ar-io-core:4000
RESOLVER_ARWEAVE_RETAINED_INTERNAL=http://ar-io-retained:4000
RESOLVER_ARWEAVE_COLD_TIMEOUT=300
RESOLVER_STATIC_ROOT=/state/media
RESOLVER_STATIC_CACHE_MAX_BYTES=1000000000
RESOLVER_ARWEAVE_RETENTION_DB=/state/arweave-retained.sqlite3
```

The generated `curio.env` contains `CURIO_APP_ROOT`, `CURIO_DATA_ROOT`, host
UID/GID, `CURIO_CURATOR_TOKEN`, `CURIO_PORT`, cache limits, and
`CURIO_PUBLIC_BASE_URL`, and `CURIO_TRUSTED_PROXY_CIDRS`. Do not add
`CURIO_LAN_ADDRESS`: Curio uses one request origin, not a configured LAN
address.

For direct requests, returned URLs derive from the request origin.
`CURIO_PUBLIC_BASE_URL` explicitly overrides it for proxied or non-request MCP
calls. Otherwise, forwarded headers remain ignored until
`CURIO_TRUSTED_PROXY_CIDRS` contains the immediate proxy's IP/CIDR range. Only
an allowlisted peer may provide a complete valid RFC `Forwarded` origin or an
`X-Forwarded-Proto` plus `X-Forwarded-Host` pair. Do not allowlist clients or
broad networks; proxy configurations must overwrite client-supplied forwarded
headers. REST, MCP results, and the MCP Host/Origin guard use this same
effective origin.

HTTP, inline data, and uploads use Curio static storage and never reach IPFS
implicitly. `CURIO_STATIC_CACHE_MAX_BYTES` bounds only evictable public
HTTP/data cache objects (LRU); `RESOLVER_STATIC_MAX_BYTES` remains the
per-object intake cap. Explicit static keeps and uploads are durable and are
not charged to the cache quota. IPFS keeps pin the canonical CID root and seed
through Kubo. Arweave keeps use retained Core hydration. Cache is distinct from
keep. HTML runtime responses remain live-dependent until dependency
capture/replay exists.

Read-only routes can be public; mutations require `Authorization: Bearer
<CURIO_CURATOR_TOKEN>`. This includes keep/pin, seed, store, and mutable
favorites/overrides.

## Installer and operator commands

The installer copies a staged application into a timestamped release directory,
atomically switches `current`, builds the resolver, starts Compose with `--wait`,
and checks ownership. On failure it stops the failed graph, restores the prior
`current` symlink, and attempts to restore the prior healthy graph. It never
runs Docker pruning, IPFS garbage collection, pin removal, or broad recursive
delete.

The wrapper is independent of the working directory:

```text
curio start | stop | restart [service] | status | logs [service]
curio health
curio version
curio update --check
curio update
curio update --version vX.Y.Z
```

Updates are operator initiated. The installed wrapper invokes its verified
bootstrap: `update --version vX.Y.Z` passes that exact tag as `CURIO_VERSION`,
while `update` selects the latest-release URL. The bootstrap verifies the
archive checksum before it runs the archived installer, which health-gates
Compose and rolls back `current` on failure. `update --check` still needs a
published `VERSION` asset. No release assets are currently published, so the
release installer and updater cannot succeed yet.

## State and backup

Back up the real XDG paths above, not historical `/etc`, `/opt`, or `/var/lib`
paths. At minimum preserve `curio.env` and the state tree, which includes:

```text
state/ipfs/
state/ar-io/                    ordinary Core state and first-install height
state/ar-io-retained/           retained Core state and first-install height
state/arweave-retained.sqlite3  Curio transaction/path retention registry
state/media/                    static objects and SQLite catalogue
state/overrides.toml
state/favorites.json
```

A cold backup should stop Curio before copying. Ordinary AR.IO cache can be
re-fetched while upstream data remains available; retained AR.IO intent, Kubo
pins/identity, static captures/uploads, and operator records can be unique.

## Acceptance checks

A qualified deployment demonstrates that all eight services become healthy,
only resolver `8090` and Kubo swarm `4001/tcp,4001/udp` publish host ports,
resolver results stay on the request Curio origin, state survives rerun,
recreation, and guest reboot, and a failed replacement install restores the
prior graph. See [appliance testing](appliance-testing.md).

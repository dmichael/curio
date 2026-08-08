# Curio appliance

Curio is a per-user Docker Compose appliance. It resolves and serves media on a
single HTTP front door; Kubo's gateway/API and AR.IO Envoy remain on the Compose
network. This document records the r81 service graph and operational limits,
not an aspirational deployment.

## Requirements and installation

Linux, Docker Engine plus the Compose plugin, and Docker access for the invoking
user are required. No command uses or installs `sudo`.

```sh
curl -fsSL https://…/install.sh | sh
# or, from a verified release tree:
appliance/install.sh
```

The bootstrap verifies `curio-appliance.tar.gz.sha256` before running the inner
installer. `CURIO_VERSION=vX.Y.Z` requires the archive package version to match
the requested tag.

The first installation creates:

```text
$XDG_CONFIG_HOME/curio/curio.env       (default ~/.config)
$XDG_DATA_HOME/curio/state/            (default ~/.local/share)
$XDG_DATA_HOME/curio/app/releases/     immutable staged releases
$XDG_DATA_HOME/curio/app/current -> releases/<version>-<nonce>
$XDG_BIN_HOME/curio                    (default ~/.local/bin)
```

`CURIO_APP_ROOT`, `CURIO_DATA_ROOT`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, and
`XDG_BIN_HOME` are supported. Roots must be absolute, non-root paths without
`.`/`..` components or whitespace. The persisted `CURIO_APP_ROOT` is read by
the wrapper, so `curio` works from any directory after a custom-root install.

An update stages a complete new release, validates Compose, then atomically
switches only `current`. It does not delete an application root or persistent
state. A rerun retains `curio.env`, `START_HEIGHT`, IPFS, AR.IO, static media,
and curator records. A deliberately non-symlink `current` is rejected rather
than replaced.

`curio.env` is mode 0600 and includes `CURIO_HOST_UID`, `CURIO_HOST_GID`, a
random `CURIO_CURATOR_TOKEN`, storage caps, and the resolver port. Set
`CURIO_PUBLIC_BASE_URL=https://curio.example` when a stable public/reverse
proxy origin is needed, especially for MCP. The resolver never trusts arbitrary
`X-Forwarded-*` headers.

## Service graph

The Compose project has six services: `resolver`, `kubo`, `ar-io-redis`,
`ar-io-core`, `ar-io-observer`, and `ar-io-envoy`.

* Resolver is the only media HTTP front door and publishes `${CURIO_PORT}:8090`.
  It proxies native `/ipfs/...` and `/arweave/...` paths and owns `/media/...`.
* Kubo publishes only TCP/UDP 4001 for deliberate swarm participation. Its
  gateway (8080) and RPC API (5001) are private.
* AR.IO Envoy (3000), core (4000), Redis, and the observer are private.
* Resolver waits for Kubo and Envoy `service_healthy`. Envoy itself has an r81
  `/ar-io/info` healthcheck; this makes that dependency satisfiable after a
  clean Compose start.

The observer is intentionally an inert, resolvable r81 compatibility service.
Envoy's STRICT_DNS observer cluster still requires it. Do not remove it or its
`TVAL_OBSERVER_*` variables merely because observer work is disabled.

The pinned r81 core configuration retains the proven retrieval graph:
`TRUSTED_NODE_URL` through Envoy, Redis chain cache,
`TRUSTED_GATEWAYS_URLS`, disabled ANS-104 indexing/unbundling,
`RUN_OBSERVER=false`, and retrieval order
`trusted-gateways,ar-io-network,chunks-offset-aware,tx-data`. Envoy retains the
trusted/fallback node, GraphQL, datasets, peer EDS, ARNS, idle-timeout, and
observer template variables. These are source-native retrieval behavior, not
optional cosmetic environment variables.

Kubo initialization is idempotent: it sets owned listen/storage/routing values
without changing identity, keys, pins, or unrelated config. It never invokes
repo GC or removes pins.

## Operations

```text
curio status | start | stop | restart [service] | logs [service]
curio health | version | update [--check|--version vX.Y.Z]
```

The wrapper always supplies the project, persisted env file, and
`current/compose.yaml`. Mutating resolver endpoints require
`Authorization: Bearer $CURIO_CURATOR_TOKEN`, including MCP mutation tools.

`/healthz` reports backend reachability separately from participation. Kubo
`id` addresses are only advertised-address evidence: Docker-private, loopback,
and special-use addresses are excluded, and even a global advertisement leaves
inbound reachability `unknown`. AR.IO r81 has no equivalent reachability proof.

## Media and retention semantics

IPFS remains IPFS: resolution returns `/ipfs`, and a successful explicit pin is
reported kept only after Kubo confirms it. A failed pin is `failed`, never
`kept`; background pin scheduling is not completion evidence.

HTTP and data sources are stored in Curio's static store, never automatically
copied into IPFS. Fetching streams into a bounded temporary file and atomically
commits an SHA-256-addressed object. Static favorites promote that exact object
to `kept`; they do not schedule an IPFS helper. Public data decoding and HTTP
metadata/media bodies have practical caps and fetch concurrency is limited.

A captured HTML file is `live-dependent`: scripts, assets, APIs, and browser
runtime dependencies have not thereby been preserved. `/keep` refuses to call
it durably kept until those dependencies have a capture design and evidence.
`/media` is served inline (no attachment Content-Disposition).

For external HTTP, every redirect target is checked. DNS answers must be
`ipaddress.is_global`; the connection target is replaced with a validated
numeric address while the original Host header, HTTPS SNI, and certificate name
are retained. This prevents the DNS-preflight/HTTP-client rebinding gap.

AR.IO r81 selected-transaction retention is **unsupported**. Its contiguous
cache cleanup uses metadata `accessTimestampMs`; setting
`CONTIGUOUS_DATA_CACHE_CLEANUP_THRESHOLD` controls ordinary age cleanup and an
unset value disables it. r81 offers no transactional per-transaction pin/retain
API. A kept-transaction registry with periodic complete reads/touches before
the threshold has not been proven crash-safe or race-free against cleanup, so
Curio must not claim it protects selected data. It records warm/cache evidence
only and returns the technical blocker.

## Backup and qualification

Back up the config directory and data root together. The latter contains Kubo
identity/pins, AR.IO first-deploy state/cache, static media, favorites,
overrides, and provenance. Recreating containers must not affect those bind
mounts.

`appliance/tests/test-appliance.sh` validates the release checksum, Compose
service graph/health dependencies/r81 variables, Kubo convergence, custom XDG
roots, safe reruns, wrapper discovery, and shell syntax. `dev/test-appliance.sh`
is a disposable-VM two-phase test covering install, persisted static media,
rerun, force recreation, dependency failure/recovery, and reboot.

not built: selected AR.IO transaction retention (needs a documented r81 transactional retention API or a crash-safe cleanup-race proof)

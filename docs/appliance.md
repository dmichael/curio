# Curio appliance specification

Status: implemented in Curio 0.2.

> **Current deployment contract.** Curio is a no-sudo, per-user appliance:
> configuration is `$XDG_CONFIG_HOME/curio/curio.env`; application releases
> are `$XDG_DATA_HOME/curio/app/releases` with an atomic `current` symlink;
> and persistent state is `$XDG_DATA_HOME/curio/state` (or the explicit
> `CURIO_*_ROOT` values). Only resolver `CURIO_PORT` (8090 by default) and
> Kubo swarm 4001 are published. Kubo, AR.IO, and Envoy gateway/admin ports
> stay on the Compose network and all resolver URLs use the single request
> origin. The installer records Arweave height from the pinned Envoy image on
> first install, waits for full Compose health, and rolls back a failed update.
> It runs state-writing containers as the installing UID:GID. Historical
> 0.1 path/port/LAN examples below are retained for qualification rationale;
> this current contract takes precedence where they differ.

This document defines the installable appliance and preserves the detailed
first-build qualification constraints that later packaging must continue to meet.

## Goal

A user with a Linux machine and Docker should be able to run:

```bash
curl -fsSL https://github.com/dmichael/curio/releases/latest/download/install.sh | bash
```

The result is a complete Curio service: resolver, local IPFS gateway and pin
store, and local Arweave gateway/cache. Running the installer again must leave
configuration and stored content intact.

The release bootstrap verifies the downloaded archive and invokes
`appliance/install.sh` from that archive. Docker Compose is the runtime
definition, and `appliance/install.sh` remains the single installation engine.
Deployment automation should invoke it rather than implement another method.

## Reference installation

The complete service has been exercised on a Pine64 Rock64 with a four-core ARM
Cortex-A53, 4 GB RAM, 128 GB eMMC, and Debian/DietPi. This is a known-good
example, not an enforced minimum.

The installer does not reject a machine based on RAM, disk size, CPU model, or
hardware class. Resource values such as Kubo's storage maximum are configurable
defaults. Health checks report what actually runs.

## Scope of the first implementation

The first implementation includes:

- `install.sh`, the release-download and checksum-verification bootstrap;
- `resolver/Dockerfile` for the existing FastAPI service;
- `appliance/compose.yaml`;
- `appliance/curio.env.example`;
- `appliance/install.sh`;
- an `appliance/curio` operator command;
- persistent bind mounts under `/var/lib/curio`;
- service health checks;
- tests for generated configuration and installer reruns where practical.

The resolver image may be built locally by Compose in this first version. A
public image registry and multi-architecture release build are later work.

The first implementation does not:

- install Docker;
- support installation profiles;
- implement automatic updates or rollback;
- delete Curio data;
- migrate an existing native installation;
- modify unrelated host infrastructure;
- configure the host firewall or router.

## Host expectation

The first installer expects:

- Linux;
- root privileges for writing system directories;
- a working Docker Engine;
- the `docker compose` plugin.

These are concrete software dependencies, not judgments about suitable
hardware. If Docker or Compose is absent, the installer stops with a useful
message and leaves the host unchanged.

The installer must not rewrite Docker daemon configuration, package sources,
firewall rules, DNS settings, or unrelated containers.

## Compose services

The first Compose project runs:

- `resolver`: Curio's FastAPI resolver;
- `kubo`: IPFS gateway and pin store;
- `ar-io-core`: the ordinary, evictable AR.IO Core used by Envoy;
- `ar-io-envoy`: the private ordinary Arweave gateway upstream;
- `ar-io-redis`: the ordinary Core's Redis dependency;
- `ar-io-retained`: a private second instance of the same pinned r81 Core,
  used only by explicit Curio keep/seed hydration;
- `ar-io-retained-redis`: the retained Core's separate Redis dependency;
- a disabled observer compatibility container only if the pinned AR.IO Envoy
  definition still requires that service to exist.

Use the pinned AR.IO r81 configuration:
observer disabled, ANS-104 unbundling and indexing disabled, first-deploy
`START_HEIGHT` retained, and on-demand retrieval preferring trusted gateways.
The appliance configures ar-io.dev and r81's Turbo gateway as trusted tiers,
then Permagate and arweave.net as explicitly untrusted fallback tiers. This
avoids making cold retrieval depend on a single upstream while preserving the
trust distinction on fallback bytes. Core prefers IPv4 DNS results so hosts
without a working IPv6 route do not exhaust the request window on IPv6
connection attempts. Site-specific warm lists, hostnames, DNS settings,
registries, and collection manifests do not belong in the appliance.

`ar-io-retained` has its own bind-mounted `/app/data` (including r81 SQLite
state), its own first-install height file, and no
`CONTIGUOUS_DATA_CACHE_CLEANUP_THRESHOLD`. Its only trusted node is the ordinary
Envoy. Curio records pending/kept/failed transaction/path intent in its own
SQLite registry, fully consumes the retained Core response, verifies a second
native response, then marks it kept. This is isolated native retained-plane
operation, **not an AR.IO r81 per-transaction pin API**. Public
`/arweave/<txid>/<path>` requests for kept txids go only to this Core; failure
is reported as degraded rather than falling back to ordinary cached bytes.

Only include AR.IO support containers that are required for this posture.
`autoheal` or access to the Docker socket must not be added without a concrete
need.

All images use explicit versions. The Compose file must not use `latest`.

## Network surface

Default host-facing ports are:

| Port | Service | Purpose |
|---|---|---|
| `3000/tcp` | AR.IO Envoy | Arweave gateway |
| `8080/tcp` | Kubo gateway | IPFS media gateway |
| `8090/tcp` | Curio resolver | REST, MCP, skills, and operator API |

The resolver, Kubo RPC API, AR.IO core, Redis, and compatibility services share
a private Compose network. Redis and AR.IO core are not published on the host.

Docker's interaction with host firewalls varies. Curio documents its published
ports but does not add or rewrite firewall rules in the first implementation.

### Kubo RPC API

Kubo's RPC API on `5001` remains private to the Compose network. It can alter
pins and daemon configuration, run repository operations, and stop Kubo. Normal
clients need the gateway on `8080` or Curio's API on `8090` instead.

The Kubo process must listen on the Compose network so the resolver can reach
it, but Compose must not publish port `5001` on the host. Direct localhost or
LAN exposure can be considered later as an explicit option.

### Kubo swarm

Kubo needs outbound network access, which Docker provides. The first Compose
file does not publish an inbound swarm port. Public IPFS-provider operation and
router forwarding are separate operator choices for later work.

## Kubo initialization

The official Kubo image needs an idempotent initialization step. On an empty
state directory it initializes the repository. On every start it ensures the
settings Curio owns are correct without replacing peer identity, keys, pins, or
unrelated state.

The first implementation manages at least:

- API listen address inside the Compose network;
- gateway listen address;
- `Datastore.StorageMax` from configuration;
- SBC-safe connection-manager defaults;
- `Routing.Type: autoclient`.

Initialization must complete before the Kubo daemon starts. It may use a small
entrypoint wrapper or a one-shot Compose service sharing the Kubo state mount.
It must not run garbage collection or remove pins.

## Filesystem layout

```text
/opt/curio/
    compose.yaml
    curio.env.example
    kubo-init.sh
    resolver/               # local resolver image build context
    VERSION

/etc/curio/
    curio.env

/var/lib/curio/
    ipfs/
    ar-io/
        start-height.env    # immutable first-deploy START_HEIGHT
    ar-io-retained/
        start-height.env    # same first-deploy height, separate Core state
        redis/
    arweave-retained.sqlite3 # Curio retained txid/path state registry
    resolver/
        overrides.toml
        favorites.json
        captures/
            captures.jsonl
            warmed.jsonl

/usr/local/bin/curio
```

The locations follow normal Linux conventions:

- `/opt/curio` contains the installed application definition;
- `/etc/curio` contains operator configuration;
- `/var/lib/curio` contains state;
- `/usr/local/bin/curio` is the operator command.

Use bind mounts rather than anonymous Docker volumes. The host paths make
backup, disk inspection, and migration understandable. Recreating containers
must not affect these directories.

The ordinary AR.IO tree is cache-like but contains indexes and first-deploy
state. The retained Core has a separate `ar-io-retained` tree so ordinary cache
cleanup cannot evict explicit keeps. The installer records the initial chain
height atomically in `ar-io/start-height.env`, initializes the retained file
from it once, and validates rather than replacing either on rerun.

The selected images' runtime users and numeric IDs must be inspected before the
installer assigns ownership. The pinned Kubo image defines `ipfs` as
`1000:100`, Redis defines `redis` as `999:1000`, and the resolver image defines
`curio` as `10001:10001`; Compose runs those services with the corresponding
numeric IDs. AR.IO r81 core performs its own startup migrations as root. The
installer changes ownership only when creating a runtime directory or when a
pre-created mount point is empty. It never recursively changes an existing
Kubo repository.

Logs use Docker's configured logging driver and are read with `curio logs`.
Curio does not create a second log archive under `/var/log`.

## Configuration

The installed environment file is `/etc/curio/curio.env`. It contains
appliance-level values, which Compose maps to the resolver's existing
`RESOLVER_*` settings.

An initial example:

```dotenv
CURIO_LAN_ADDRESS=192.168.1.50
CURIO_DATA_ROOT=/var/lib/curio
CURIO_IPFS_STORAGE_MAX=20GB
```

Compose supplies the internal service addresses:

```text
RESOLVER_IPFS_INTERNAL=http://kubo:8080
RESOLVER_IPFS_API=http://kubo:5001
RESOLVER_ARWEAVE_INTERNAL=http://ar-io-envoy:3000
```

It derives consumer-facing gateway bases from `CURIO_LAN_ADDRESS` and maps the
resolver's override, favorite, and capture paths into its bind-mounted state
directory.

On first install, `appliance/install.sh` suggests a LAN address and allows the
operator to accept or replace it. The bootstrap runs this inner installer from
the verified release archive rather than through standard input, so it can
prompt through the terminal. `CURIO_LAN_ADDRESS` supplied to the bootstrap's
`bash` process bypasses the prompt.

The installer never replaces an existing `/etc/curio/curio.env`. New Compose
settings need defaults so an older environment file remains usable.

Curio uses a LAN IP rather than an mDNS name because some renderers, including
the FF1, do not resolve `.local` names.

## Installer behavior

`appliance/install.sh`:

1. checks Linux, root execution, Docker, and `docker compose`;
2. determines the repository or unpacked release directory containing the
   appliance files;
3. creates `/opt/curio`, `/etc/curio`, and `/var/lib/curio` as needed;
4. installs the Compose file and operator command;
5. creates `curio.env` only when absent;
6. creates only the required empty state directories;
7. builds or pulls the explicitly versioned images;
8. runs `docker compose up -d`;
9. waits for local component health and Curio's `/healthz`;
10. prints the three LAN URLs and common commands.

A rerun converges the installed Compose and command files while preserving
configuration and state. It must not require manual cleanup after an interrupted
run.

Errors identify the failed command and leave the existing state in place. The
script must not use Docker volume pruning, system pruning, IPFS garbage
collection, or broad recursive deletion.

## Operator command

`/usr/local/bin/curio` always calls Compose with the installed project and
environment files. Its behavior does not depend on the current directory.

The first commands are:

```text
curio start
curio stop
curio restart [service]
curio status
curio health
curio logs [service]
```

Unknown service names and missing installations produce clear errors. `logs`
passes through useful follow/follow-tail options rather than inventing a second
logging interface.

Update and uninstall commands are deferred until their state and failure
semantics are specified and tested.

## Health

Each long-running service has a focused Compose health check where the upstream
image provides the necessary tool. Curio's existing `/healthz` is the aggregate
user-facing result.

A successful installation demonstrates that:

- the resolver answers on `8090`;
- the resolver can reach Kubo's gateway and RPC API;
- the resolver can reach AR.IO Envoy;
- AR.IO Envoy can reach core;
- returned public gateway URLs use `CURIO_LAN_ADDRESS`;
- only the documented host ports are published.

Health reports observed service state. It does not estimate performance from
hardware facts.

## State and backup classes

Curio state has different recovery value:

- operator state: overrides, favorites, and provenance ledgers;
- unique content: captured originals and locally supplied masters stored in
  Kubo;
- reproducible content: references that can be seeded again from surviving
  networks and source metadata;
- cache data: AR.IO payloads that can be fetched again while upstream sources
  remain available.

A complete cold backup stops Curio and copies `/etc/curio` and
`/var/lib/curio`. Later documentation should also offer smaller backups, but it
must explain that Kubo may contain unique captured bytes as well as reproducible
pins. Kubo identity and IPNS keys are part of the IPFS state tree.

Backup tooling is not part of the first implementation.

## Acceptance criteria

The first implementation is complete when:

1. The release bootstrap installs Curio on a clean Linux host that already has
   Docker and Compose, after verifying the archive checksum.
2. `3000`, `8080`, and `8090` answer on the configured address.
3. Kubo RPC, Redis, and AR.IO core are not published on the host.
4. Running the installer twice preserves configuration and state.
5. Curio returns after a host reboot through Docker restart policies.
6. Recreating the containers preserves pins, captures, overrides, favorites,
   and AR.IO first-deploy state.
7. The installer does not alter unrelated Docker configuration or containers.
8. A failed health check identifies the component that failed.
9. The Compose definition is valid on AMD64 and ARM64; the known ARM64 host is
   a deployment target, not a place to run development tools or ad hoc tests.
10. Existing resolver tests continue to pass.

Qualify the bundle in a disposable VM before installing it on a production
host.

## Later work

Once fresh install, rerun, reboot, and state retention are proven:

- publish multi-architecture resolver images;
- define `curio update` with version and failure semantics;
- define uninstall while retaining state by default;
- document full and selective backup and restore;
- consider optional direct Kubo RPC and inbound swarm exposure;
- consider a reduced installation without AR.IO if there is a real use case;
- plan migration of existing native deployments without re-fetching or copying
  data unnecessarily.

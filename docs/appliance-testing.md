# Testing the Curio appliance without a physical device

Curio should be qualified in disposable Linux virtual machines before it is
installed on an SBC. A container or Docker-in-Docker test alone is not enough:
it does not faithfully exercise installation into `/opt`, `/etc`, and
`/var/lib`, bind-mount ownership, host port publication, Docker restart
policies, or persistence across a guest reboot.

The goal is not merely to see six green containers. The qualification process
must prove clean installation, safe convergence after failure, network
isolation, durable state, component-specific health reporting, container
replacement, and reboot recovery on both supported architectures.

## Safety boundary

Use a VM that can be deleted in full. Do not use a production deployment as a
test bench.

The automated integration script refuses to run unless Linux reports that it is
virtualized, and it also requires an explicit destructive-test flag:

```bash
dev/test-appliance.sh --disposable-vm
```

It writes the real appliance paths and deliberately stops Kubo during a failure
test. Treat the guest as disposable even though the script does not delete data
or prune Docker.

## Confidence layers

Run the layers in this order. A later layer does not replace an earlier one.

| Layer | Environment | What it proves |
|---|---|---|
| Resolver tests | Developer machine or CI | Existing application behavior remains intact |
| Appliance static tests | Any host with the Docker CLI and Compose plugin | Compose renders, image references and mounts are correct, ports remain private, rerun/config and operator-wrapper behavior are correct |
| Image manifest check | Registry access only | Every pinned upstream image has AMD64 and ARM64 manifests |
| Full VM test | Fresh Linux VM | Real filesystem installation, ownership, image pulls/build, startup, APIs, and host port mappings |
| Fault and persistence test | Same disposable VM | Failed-install rerun, failed-component health, container recreation, and state retention |
| Reboot test | Same disposable VM | Docker restart policies and host persistence work after boot |
| Cross-architecture repeat | One AMD64 VM and one ARM64 VM | No architecture-specific image or runtime assumptions remain |

Do not move to physical hardware until every required layer passes for the exact
revision under review.

## Fast static suite

This suite does not start containers or need a Docker daemon. Compose itself is
used as the renderer, so the test sees interpolation and normalization rather
than attempting to parse YAML independently.

```bash
./appliance/tests/test-appliance.sh

cd resolver
pytest
ruff check .
cd ..

git diff --check
```

`test-appliance.sh` validates at least:

- the exact six-service definition renders;
- only `3000/tcp`, `8080/tcp`, and `8090/tcp` are published;
- Kubo RPC, AR.IO core, Redis, and the inert observer are private;
- mutable bind mounts resolve under `CURIO_DATA_ROOT`;
- no service mounts the Docker socket or runs privileged;
- image references are versioned rather than `latest`;
- Kubo and resolver internal addresses are mapped correctly;
- Kubo's pre-daemon configuration hook makes no writes on a second run and
  contains no garbage-collection or pin-removal command;
- AR.IO's observer/unbundling/indexing/cache posture is present;
- an existing `curio.env` is not overwritten;
- `curio` constructs Compose commands with the installed file paths from an
  unrelated current directory;
- unknown operator service names are rejected.

These checks run quickly and should be part of every normal CI run.

## Verify multi-architecture image availability

Tags being syntactically versioned is not proof of architecture support. Before
a release, inspect each image in `appliance/compose.yaml` and the resolver's
Python base image:

```bash
docker buildx imagetools inspect ipfs/kubo:v0.40.1
docker buildx imagetools inspect redis:7.4.7-alpine
docker buildx imagetools inspect \
  ghcr.io/ar-io/ar-io-core:f3032933c6039305bc5ecec0d486526c6d60d6ea
docker buildx imagetools inspect \
  ghcr.io/ar-io/ar-io-envoy:bd738a2435f1293e259dfcbb4ef42f50b26545da
docker buildx imagetools inspect \
  ghcr.io/ar-io/ar-io-observer:308b6777d0df4a45f59a1984bcb0874a50f58965
docker buildx imagetools inspect python:3.13.11-slim-bookworm
```

Each output must contain `linux/amd64` and `linux/arm64`. Curio pins the
multi-platform index digest as well as the human-readable tag, so record the
observed index digest in test evidence and compare it with the source.

## Recommended VM matrix

Use at least these two guests:

| Guest | Suggested source | Purpose |
|---|---|---|
| ARM64 Linux, 4 CPUs, 4-6 GB RAM, 40-60 GB disk | Lima on Apple Silicon, UTM, or an ARM cloud VM | Matches the architecture and resource class of the reference SBC |
| AMD64 Linux, 4 CPUs, 4-6 GB RAM, 40-60 GB disk | Local hypervisor or ephemeral cloud VM | Exercises the other supported architecture |

A current Ubuntu LTS or Debian release is appropriate. The VM must have Docker
Engine and the Compose plugin before testing starts; that provisioning is
outside `install.sh` and should be part of the reproducible VM template.

On an Apple Silicon Mac, use the checked-in Lima definition:

```bash
brew install lima
limactl start --name=curio-appliance-test dev/lima.yaml
```

`dev/lima.yaml` creates a native ARM64 guest with rootful Docker, 4 CPUs, 6 GiB
of memory, and 60 GiB of disk. The checkout is inherited from Lima's read-only
home mount. Curio's `/opt`, `/etc`, and `/var/lib` trees remain on the guest's
native filesystem. Host ports 13000, 13080, and 13090 forward to guest ports
3000, 8080, and 8090 respectively.

Lima is a developer dependency and Curio never installs it. Keep appliance state
on the guest's native filesystem, not on a macOS-shared mount, because ownership
and atomic replacement semantics are part of the test.

Take a VM snapshot immediately after Docker is installed and before Curio is
run. Reverting to that snapshot is the clean-install reset; manually deleting a
prior installation is not equivalent evidence.

## Automated full-VM qualification

Inside a fresh guest, run from the checkout as the ordinary administrative
user:

```bash
./dev/test-appliance.sh --disposable-vm
```

With the checked-in Lima VM, the equivalent host-side command from the
repository root is:

```bash
limactl shell curio-appliance-test -- \
  bash -lc "cd '$PWD' && ./dev/test-appliance.sh --disposable-vm"
```

The script performs the following sequence against real installation paths:

1. verifies that it is running in a Linux VM;
2. discovers the guest LAN address;
3. runs the appliance installer and waits for aggregate health;
4. verifies active Docker port bindings, including the absence of private
   service ports, and calls all three public services through the configured
   guest LAN address;
5. stores a unique local payload through `/store`, recording its CID and
   provenance;
6. creates a favorite and an operator override referencing that CID;
7. reruns the installer and compares hashes of `curio.env` and AR.IO's
   `start-height.env`;
8. force-recreates every container without removing bind-mounted state;
9. verifies the Kubo pin and bytes, resolver files, and API-visible operator
   state;
10. stops Kubo, requires `curio health` to fail and name Kubo, then recovers the
    stack;
11. leaves evidence under `/var/tmp/curio-appliance-test`.

For release qualification, also supply a small public Arweave fixture and its
known SHA-256 checksum. This tests cold on-demand retrieval through Envoy and
core, not only `/ar-io/info`, and verifies the same bytes after recreation:

```bash
CURIO_TEST_ARWEAVE_TXID='<public-test-transaction-id>' \
CURIO_TEST_ARWEAVE_SHA256='<expected-sha256>' \
  ./dev/test-appliance.sh --disposable-vm
```

The fixture must be public, unrelated to a private wallet or collection, small
enough for repeatable CI use, and content-stable. Record its identity and hash
with the qualification evidence rather than embedding private collection data
in this repository.

## Reboot phase

After the first phase passes, reboot the guest without stopping Curio first:

```bash
sudo reboot
```

For Lima, initiate the reboot from the host and wait for `limactl shell` to
become available again:

```bash
limactl shell curio-appliance-test -- sudo reboot
```

When it returns, use the same checkout:

```bash
./dev/test-appliance.sh --after-reboot
```

Or run the second phase from the host repository root:

```bash
limactl shell curio-appliance-test -- \
  bash -lc "cd '$PWD' && ./dev/test-appliance.sh --after-reboot"
```

This phase requires all containers to return through their restart policies and
rechecks health, published ports, the locally stored bytes and pin, favorites,
overrides, provenance ledger, appliance configuration hash, and AR.IO
first-deploy-height hash.

A simple `docker compose restart` is not a reboot test. The Docker daemon and
containers must actually pass through guest shutdown and startup.

## Manual failure cases

Run these in additional reverted VM instances so one case cannot mask another:

### Occupied host port

Bind one of 3000, 8080, or 8090 before the first install. Installation must fail
with the Docker command visible, retain the newly created configuration and
state, and succeed after the conflicting process is removed and the same
installer is rerun. The retained `curio.env` and `start-height.env` must be
byte-identical.

### No chain-height network access

Block only the guest's outbound request to `https://arweave.net/info` before a
first install. The installer must fail before inventing an empty or zero start
height. Restore access and rerun; a valid state file should then be created.
Once it exists, block the lookup again and confirm installer reruns do not make
the request.

### Damaged configuration

Create an existing `/etc/curio/curio.env` with an invalid LAN address. The
installer must report the specific configuration problem and must not replace
the file. Repair it explicitly, then rerun.

### Dependency failure

Stop Kubo, AR.IO Envoy, AR.IO core, and Redis one at a time. `curio health` must
exit nonzero and name the stopped or unhealthy service. Restore each component
before testing the next one.

### Interrupted execution

Interrupt one run during pull or build, then rerun without deleting containers,
images, or files. The second run must converge. Confirm that no test or
installer command invokes Docker pruning, IPFS garbage collection, or recursive
state deletion.

## Network verification

Check both the rendered model and active Docker bindings:

```bash
sudo docker compose --project-name curio \
  --env-file /etc/curio/curio.env \
  --file /opt/curio/compose.yaml config

sudo docker compose --project-name curio \
  --env-file /etc/curio/curio.env \
  --file /opt/curio/compose.yaml ps
```

Expected host ports are exactly:

| Port | Service |
|---|---|
| `3000/tcp` | AR.IO Envoy |
| `8080/tcp` | Kubo gateway |
| `8090/tcp` | Curio resolver |

Inspecting a port from another LAN namespace provides stronger evidence than a
loopback request. From the host or a second test VM, call all three guest URLs.
Also resolve a test CID and confirm that `resolved_url` uses the configured LAN
address rather than a container name, loopback address, or mDNS name.

## Evidence and release gate

Retain this evidence for each architecture:

- Curio revision and guest OS/architecture;
- Docker and Compose versions;
- rendered Compose JSON;
- image index digests and platform lists;
- first-install, rerun, forced-recreation, induced-failure, and post-reboot
  health output;
- active container inspection showing host port bindings;
- hashes of `curio.env`, `start-height.env`, and the local persistence payload;
- the stored CID and proof that it remains pinned;
- resolver test, Ruff, appliance test, and `git diff --check` output.

A revision is ready for an SBC only when fresh ARM64 and AMD64 guests both pass,
the post-reboot phase passes, all static and resolver tests pass, and every
failure injection leaves a rerunnable installation. The first physical install
is then deployment validation, not the place where packaging defects are
discovered.

## Resetting the sandbox

Delete or revert the whole VM. For Lima this means stopping and deleting the
named instance, for example:

```bash
limactl stop curio-test
limactl delete curio-test
```

Do not present manual cleanup inside a reused VM as clean-install evidence.

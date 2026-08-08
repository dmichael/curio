# Testing the Curio appliance without a physical device

Qualify Curio in disposable Linux VMs before an SBC or production deployment.
Docker-in-Docker does not prove a per-user XDG install, bind-mount ownership,
Docker restart policies, host port publication, or survival across a guest
reboot.

## Safety boundary

Use a VM that can be deleted. The integration script refuses a non-virtualized
Linux host and requires an explicit acknowledgement:

```bash
./dev/test-appliance.sh --disposable-vm
```

It installs as the ordinary guest user, writes that user's XDG paths, stops
services during fault tests, and does not use `sudo`. Treat the guest as
disposable even though the script does not prune Docker or delete state.

## Confidence layers

| Layer | What it proves |
|---|---|
| Resolver tests | Resolver behavior and media-model contracts |
| Appliance static tests | Bootstrap, rendered Compose graph, XDG installer/wrapper, rollback mechanics |
| Image manifest check | Pinned upstream images support AMD64 and ARM64 |
| Fresh VM test | Real user ownership, startup, API, state, and host bindings |
| Fault/recreation test | Failed health, rerun, forced recreation, and state retention |
| Real reboot test | Docker restart policies and host persistence after guest shutdown/start |
| Cross-architecture repeat | No architecture-specific runtime assumptions |

Run the static suite from the repository root:

```bash
./appliance/tests/test-appliance.sh
cd resolver
pytest
ruff check .
cd ..
git diff --check
```

The static appliance suite renders exactly this eight-service graph:
`resolver`, `kubo`, `ar-io-redis`, `ar-io-core`, `ar-io-retained-redis`,
`ar-io-retained`, `ar-io-observer`, and `ar-io-envoy`. It verifies pinned
images, private AR.IO/Kubo admin planes, user-owned mutable mounts, the
ordinary/retained Core split, atomic install rollback, and no destructive
pruning or pin-removal command.

Its expected host-published ports are only:

| Port | Service | Purpose |
|---|---|---|
| `8090/tcp` | resolver | the sole public Curio API/MCP/media/IPFS/Arweave origin |
| `4001/tcp` | Kubo | IPFS swarm participation |
| `4001/udp` | Kubo | IPFS swarm participation |

Kubo gateway/API and all AR.IO/Redis/Envoy ports must have no host binding.

## Image manifests

Before release qualification, inspect the pinned images in
`appliance/compose.yaml` and the resolver base image. Each must show both
`linux/amd64` and `linux/arm64`; record the index digest in qualification
evidence. The static test ensures references are pinned, but a tag being
syntactically versioned does not prove multi-architecture availability.

```bash
docker buildx imagetools inspect ipfs/kubo:v0.40.1
docker buildx imagetools inspect redis:7.4.7-alpine
docker buildx imagetools inspect \
  ghcr.io/ar-io/ar-io-core:f3032933c6039305bc5ecec0d486526c6d60d6ea
docker buildx imagetools inspect \
  ghcr.io/ar-io/ar-io-envoy:bd738a2435f1293e259dfcbb4ef42f50b26545da
docker buildx imagetools inspect \
  ghcr.io/ar-io/ar-io-observer:308b6777d0df4a45f59a1984bcb0874a50f58965
```

## Recommended VM matrix

Use a fresh ARM64 Linux VM (four CPUs, 4–6 GiB RAM, 40–60 GiB disk) and an
AMD64 VM with the same broad resources. Docker Engine and Compose are VM-image
prerequisites, not installer responsibilities. Snapshot each VM after Docker
is ready and before Curio runs; reverting that snapshot is clean-install
evidence.

On Apple Silicon, the checked-in Lima definition creates an ARM64 rootful-Docker
guest:

```bash
brew install lima
limactl start --name=curio-appliance-test dev/lima.yaml
```

The checkout comes through Lima's read-only home mount; Curio state must remain
on the guest's native filesystem. The definition forwards only host `13090` to
guest `8090` for optional host-side HTTP evidence. It does not forward private
native gateway ports or Kubo swarm.

### Lima 2.2 reboot caveat

Lima 2.2 static SSH port forwards can remain stale or disappear across an
**in-guest** reboot even after `limactl shell` works again. Do not replace the
real guest reboot test with a Lima lifecycle restart: the point of that test is
to exercise Docker and Curio through guest shutdown/start. Run the in-guest
reboot and `--after-reboot` checks first. If host-forward evidence is also
needed afterwards, use `limactl stop curio-appliance-test` followed by
`limactl start curio-appliance-test`; then repeat the host-forward check. Record
that lifecycle repair separately from the real reboot result.

## Full VM qualification

Inside a fresh guest as its ordinary administrative user:

```bash
./dev/test-appliance.sh --disposable-vm
```

From the host with the checked-in Lima VM:

```bash
limactl shell curio-appliance-test -- \
  bash -lc "cd '$PWD' && ./dev/test-appliance.sh --disposable-vm"
```

The script installs the source checkout, waits for aggregate health, verifies
only the allowed host bindings, fetches a public Arweave fixture through
`/arweave`, explicitly keeps it through `POST /keep`, proves the retained route
does not fall back to ordinary Core/Envoy bytes, uploads static media through
`POST /store`, records favorite/override state, reruns the installer,
force-recreates the graph, and injects a Kubo failure.

Supply a public, small, content-stable Arweave fixture and checksum for release
evidence (the defaults in the script are a public fixture):

```bash
CURIO_TEST_ARWEAVE_TXID='<public-test-transaction-id>' \
CURIO_TEST_ARWEAVE_SHA256='<expected-sha256>' \
  ./dev/test-appliance.sh --disposable-vm
```

The fixture must be unrelated to a private collection. This tests native cold
retrieval and retained-plane routing rather than only an AR.IO info endpoint.

## Reboot phase

After the first phase succeeds, reboot the guest without stopping Curio:

```bash
sudo reboot
```

For Lima:

```bash
limactl shell curio-appliance-test -- sudo reboot
```

Wait for the guest, then run in that same guest:

```bash
./dev/test-appliance.sh --after-reboot
```

or:

```bash
limactl shell curio-appliance-test -- \
  bash -lc "cd '$PWD' && ./dev/test-appliance.sh --after-reboot"
```

This checks aggregate health, only the allowed published ports, static bytes,
retained Arweave bytes, favorites, configuration, and first-install height. A
`docker compose restart` is not reboot evidence.

## Manual fault cases

In separately reverted VMs, test an occupied `8090` or `4001` binding, blocked
first-install chain-height access, malformed existing `curio.env`, each
component stopped in turn, and an installer interrupted during pull/build. A
rerun must preserve XDG configuration and state, and no path may invoke Docker
pruning, IPFS GC, or recursive state deletion.

Inspect the active graph with the actual user paths:

```bash
docker compose --project-name curio \
  --env-file "$XDG_CONFIG_HOME/curio/curio.env" \
  --file "$XDG_DATA_HOME/curio/app/current/compose.yaml" ps
```

When custom roots are configured, use the values from `curio.env` instead.
Confirm that a `/resolve` response uses the request Curio origin, not a
container name, localhost, configured LAN address, or an upstream gateway.

## Evidence and reset

Keep the revision, guest OS/architecture, Docker/Compose versions, rendered
Compose JSON, image platform evidence, install/rerun/recreation/fault/reboot
health output, active port bindings, XDG state ownership, fixture checksum,
and resolver/static-test output. Delete or revert the entire VM for a new clean
install; manual cleanup in a reused guest is not equivalent evidence.

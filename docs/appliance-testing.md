# Appliance testing

Use disposable Linux VMs for appliance qualification. Container-only tests do
not cover host ownership, restart policies, or reboot recovery.

## Local checks

```bash
./appliance/tests/test-appliance.sh
cd resolver
pytest
ruff check .
cd ..
git diff --check
```

The appliance test renders the three-service Compose file and checks mounts,
ports, image pins, installer rollback, and the absence of destructive cleanup
commands.

## VM run

The supplied Lima definitions create ARM64 and emulated AMD64 qualification
guests on Apple Silicon:

```bash
limactl start --name=curio-appliance-arm64 dev/lima.yaml
limactl shell curio-appliance-arm64

limactl start --name=curio-appliance-amd64 dev/lima-amd64.yaml
limactl shell curio-appliance-amd64
```

The AMD64 QEMU guest is substantially slower than the native ARM64 VZ guest
and requires `brew install lima-additional-guestagents` on the macOS host.

Inside a fresh VM:

```bash
./dev/test-appliance.sh --disposable-vm
```

The script checks:

- installation as an ordinary user;
- health of the resolver, Kubo, and AR.IO Core;
- first Arweave fetch and a later `X-Cache: HIT`;
- static media and IPFS state after container recreation;
- AR.IO state after container recreation;
- failure and recovery of Kubo and AR.IO Core;
- installer reruns without replacing configuration;
- ownership of persistent files.

### Release and update path

Before tagging a public release, create temporary candidate and update-fixture
tags on the exact commits to qualify. Their tag names must match the package
versions. Stage them into a GitHub-compatible local release tree:

```bash
./dev/stage-test-releases.sh /tmp/curio-releases v0.2.2 \
  v0.2.0 v0.2.1 v0.2.2
python3 -m http.server 18080 --bind 0.0.0.0 --directory /tmp/curio-releases
```

Make that release root reachable from the guest, then run the same VM test with
release mode enabled. From Lima, a server on the macOS host is normally
`host.lima.internal`:

```bash
CURIO_TEST_RELEASE_BASE_URL=http://host.lima.internal:18080 \
CURIO_TEST_INSTALL_VERSION=v0.2.0 \
CURIO_TEST_UPDATE_VERSION=v0.2.1 \
CURIO_TEST_LATEST_VERSION=v0.2.2 \
  ./dev/test-appliance.sh --disposable-vm
```

This downloads the shipped bootstrap, verifies and installs the first archive,
checks for the staged latest version, exact-updates to `v0.2.1`, then follows
the immutable latest pointer to `v0.2.2`. It reruns all state and ownership
checks after each update.

Optional fixture versions add the destructive failure cases:

```bash
CURIO_TEST_RELEASE_BASE_URL=http://host.lima.internal:18080 \
CURIO_TEST_INSTALL_VERSION=v0.2.0 \
CURIO_TEST_UPDATE_VERSION=v0.2.1 \
CURIO_TEST_LATEST_VERSION=v0.2.2 \
CURIO_TEST_REJECT_VERSION=v0.1.9 \
CURIO_TEST_FAILED_UPDATE_VERSION=v0.2.3 \
  ./dev/test-appliance.sh --disposable-vm
```

Stage all five download directories before starting the VM. The reject fixture
must include the bootstrap and archive but publish an invalid archive checksum;
the test requires the checksum-mismatch diagnostic. The failed-update fixture
must pass archive verification but make the resolver health check fail; the
test requires Compose startup and rollback diagnostics. A missing asset or an
archive rejected before startup does not satisfy either destructive check.

After either VM path passes, reboot the VM and run:

```bash
./dev/test-appliance.sh --after-reboot
```

This confirms that the services and stored media return after a real guest
reboot.

The default public Arweave fixture can be replaced with another small, stable
transaction:

```bash
CURIO_TEST_ARWEAVE_TXID='<transaction-id>' \
CURIO_TEST_ARWEAVE_SHA256='<sha256>' \
  ./dev/test-appliance.sh --disposable-vm
```

## Expected ports

| Port | Purpose |
|---|---|
| `8090/tcp` | Curio HTTP origin |
| `4001/tcp` | IPFS swarm |
| `4001/udp` | IPFS swarm |

No Kubo HTTP or AR.IO port should be bound on the host.

Before a release, run the VM test on ARM64 and AMD64. Confirm every pinned base
image supports both architectures:

```bash
./scripts/check-image-platforms.sh
```

Lima 2.2 may lose its host-side SSH port forward after an in-guest reboot even
when Curio is healthy. Run the post-reboot checks inside the guest first. A
Lima stop/start restores its development host forward if separate host-side
evidence is needed.

# Testing the Curio appliance without a physical device

Qualify Curio in disposable Linux VMs before an SBC or production deployment.
Docker-in-Docker does not prove a per-user XDG install, bind-mount ownership,
restart policies, host port publication, or survival across a guest reboot.

## Safety boundary

Use a VM that can be deleted:

```bash
./dev/test-appliance.sh --disposable-vm
```

The script refuses a non-virtualized Linux host, installs as the ordinary guest
user, and does not use `sudo` except for the separately requested guest reboot.

## Static checks

```bash
./appliance/tests/test-appliance.sh
cd resolver
pytest
ruff check .
cd ..
git diff --check
```

The appliance static suite renders exactly the three-service graph: `resolver`,
`kubo`, and `ar-io-core`. It verifies pinned images, Core's LMDB/direct
Arweave configuration, cleanup disabled, private AR.IO/Kubo admin planes,
user-owned mounts, no obsolete graph dependency, upgrade
`--remove-orphans`, atomic rollback, and no destructive pruning or pin removal.

Only these ports may be published:

| Port | Service | Purpose |
|---|---|---|
| `8090/tcp` | resolver | Curio API/MCP/media/IPFS/Arweave origin |
| `4001/tcp` | Kubo | IPFS swarm participation |
| `4001/udp` | Kubo | IPFS swarm participation |

Inspect the pinned resolver base, Kubo, and AR.IO Core images for both
`linux/amd64` and `linux/arm64` before release qualification.

## VM qualification

Inside a fresh Linux guest:

```bash
./dev/test-appliance.sh --disposable-vm
```

The script installs the checkout and verifies first AR.IO fetch, a subsequent
native `X-Cache:HIT`, explicit same-Core keep fetch/verification, state after
installer rerun and forced recreation, Core failure/recovery, Kubo
failure/recovery, and static/favorite/configuration persistence. It records a
public fixture checksum as evidence. Keep is not treated as Arweave-network
replication.

Use an unrelated, small, public stable fixture if overriding the defaults:

```bash
CURIO_TEST_ARWEAVE_TXID='<public-test-transaction-id>' \
CURIO_TEST_ARWEAVE_SHA256='<expected-sha256>' \
  ./dev/test-appliance.sh --disposable-vm
```

After the first phase, reboot the guest without stopping Curio, then run:

```bash
./dev/test-appliance.sh --after-reboot
```

This repeats health, bindings, static bytes, Arweave cache availability,
favorites, configuration, and first-install-height checks. A Compose restart is
not reboot evidence.

Manual fault cases should include blocked first-install height access, malformed
existing `curio.env`, occupied host ports, each component stopped in turn, and
an installer interrupted during pull/build. A rerun must preserve state and
configuration; obsolete historical directories/configuration keys are left
untouched.

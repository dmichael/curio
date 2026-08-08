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

After it passes, reboot the VM and run:

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

Before a release, run the VM test on ARM64 and AMD64 and confirm that each
pinned image has a manifest for both architectures.

Lima 2.2 may lose its host-side SSH port forward after an in-guest reboot even
when Curio is healthy. Run the post-reboot checks inside the guest first. A
Lima stop/start restores its development host forward if separate host-side
evidence is needed.

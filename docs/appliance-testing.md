# Appliance qualification

Run the non-destructive qualification from a checkout:

```sh
appliance/tests/test-appliance.sh
```

It uses a temporary XDG tree and fake Docker only for installer/wrapper
invocations, while using `docker compose config` when Docker is available. It
checks archive checksum rejection, requested release-version verification,
custom `CURIO_APP_ROOT` discovery, release/current-pointer reruns, persistent
state preservation, shell syntax, Kubo convergence, and no broad application
root replacement.

Compose qualification asserts the full r81 graph: resolver, Kubo, Redis, core,
Envoy, and the resolvable disabled observer; Kubo and Envoy health dependencies;
Envoy's actual `/ar-io/info` healthcheck; private gateway/backend ports; Kubo
swarm ports; and the core/Envoy retrieval variables. It is intentionally not a
few grep checks because an omitted Envoy healthcheck prevents a clean start.

## Disposable VM

On a VM owned by the invoking Docker user:

```sh
dev/test-appliance.sh --disposable-vm
# reboot the VM
dev/test-appliance.sh --after-reboot
```

The first phase installs into XDG roots, stores authenticated static media,
checks health, reruns the installer, force-recreates the Compose services,
stops Kubo to prove health fails, starts it again, and saves evidence under
`$XDG_STATE_HOME/curio-appliance-test` (or `CURIO_TEST_EVIDENCE_DIR`). The
second phase verifies that the environment file, AR.IO first-deploy height,
static media, and favorite state survived reboot.

The VM test pulls/builds images and therefore is not suitable for a laptop or
shared host. It requires systemd virtualization detection, Docker, Compose,
curl, Python, and no `sudo`.

## Resolver coverage

Run:

```sh
cd resolver
uv run --extra dev pytest -q
uv run --extra dev ruff check .
```

Coverage includes per-hop redirect SSRF rejection, numeric DNS pinning with
original Host/SNI identity, bounded static/data handling, failed IPFS pins,
static favorite promotion without IPFS ingestion, live-dependent HTML,
configured MCP origin, inline media, and honest IPFS participation evidence.

not built: selected AR.IO transaction retention (needs a documented r81 transactional retention API or a crash-safe cleanup-race proof)

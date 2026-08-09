# Appliance

Curio installs as the current Linux user. Docker Engine and the Compose plugin
must already work for that user. The installer does not use `sudo`, install
Docker, or alter firewall and network settings.

## Files

Defaults follow XDG paths:

```text
~/.config/curio/curio.env             configuration
~/.local/share/curio/app/releases/    installed application copies
~/.local/share/curio/app/current      active release symlink
~/.local/share/curio/state/           persistent state
~/.local/bin/curio                    operator command
```

Set `CURIO_APP_ROOT` or `CURIO_DATA_ROOT` before the first install to use other
absolute, non-root paths.

## Services

The Compose file runs three services:

| Service | Purpose |
|---|---|
| `resolver` | REST, MCP, static media, and the public media routes |
| `kubo` | IPFS fetch, serving, pinning, and peer traffic |
| `ar-io-core` | Arweave fetch, persistent cache, and serving |

AR.IO Core uses embedded LMDB and talks directly to Arweave nodes and gateways.
It does not need Redis, Envoy, or Observer. Its automatic content cleanup is
disabled.

## Ports

The resolver publishes port 8090 by default. This is the only public HTTP
origin. Kubo publishes port 4001 over TCP and UDP for IPFS peers. Kubo's HTTP
ports and AR.IO Core are private to Compose.

## Installation and updates

From a checkout:

```bash
./appliance/install.sh
```

The installer writes a new application directory, switches the `current`
symlink, and waits for all three services. If startup fails, it restores the
previous symlink and deployment. It leaves persistent state alone.

The operator command supports:

```text
curio status
curio health
curio logs resolver --follow
curio version
curio update --check
curio update
curio update --version vX.Y.Z
```

No release assets have been published yet, so remote installation and update
commands cannot complete until the first release exists.

## Configuration

Common settings in `curio.env` include:

```text
CURIO_PORT=8090
CURIO_IPFS_STORAGE_MAX=20GB
CURIO_STATIC_CACHE_MAX_BYTES=1000000000
CURIO_ARWEAVE_COLD_TIMEOUT=300
CURIO_PUBLIC_BASE_URL=
CURIO_TRUSTED_PROXY_CIDRS=
```

The installer also records the current user and group IDs. Curio has no user
authentication and should remain on a trusted network.

## Backup

Stop Curio before a cold backup, then copy `curio.env` and the state directory.
The important state is Kubo's repository, AR.IO Core's data, static media,
resolution records, favorites, and overrides.

Old directories left by earlier development builds are not deleted during an
upgrade. Remove them manually only after confirming that the current Compose
file does not mount them.

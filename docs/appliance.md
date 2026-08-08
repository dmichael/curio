# Curio appliance

The appliance is a per-user Docker Compose installation. It requires Linux and
a Docker runtime the installing user can access. It does not request elevated
privileges, alter system configuration, install Docker, change firewall rules,
or use world-writable state.

Default paths are `$XDG_DATA_HOME/curio/app`, `$XDG_CONFIG_HOME/curio`, and
`$XDG_DATA_HOME/curio/state`. They may be overridden with the documented XDG
variables or `CURIO_APP_ROOT` and `CURIO_DATA_ROOT`.

Only the Curio front door is published by default (`CURIO_PORT`, 8090 by
default). Kubo's gateway/API and AR.IO's gateway remain on the private Compose
network and are proxied as `/ipfs/...` and `/arweave/...`. Kubo's swarm port is
an intentional protocol participation endpoint, not a consumer media URL.

Kubo and AR.IO are enabled in the standard Compose definition. Pinned IPFS
DAGs are native retained content. AR.IO r81 provides cache observation but no
documented selected-transaction eviction protection API; Curio does not label
an AR.IO cache warm as durable retention.

The installed `curio` command supports `version`, `update --check`, `update`,
and `update --version vX.Y.Z`. Updates are never automatic. The release
bootstrap verifies its archive checksum, and a requested tag must agree with
the package version inside that archive.

not built: verified rollback (needs a retained previous-release policy)

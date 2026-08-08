# Security

## Deployment boundary

Curio can be placed behind a private-network or internet-facing front door; a
network boundary is not authorization. Its one public HTTP origin is port
`8090`, which serves REST, MCP, static media, and proxied IPFS/Arweave paths.
Kubo and the ordinary and retained AR.IO planes are internal Compose services.
Kubo swarm `4001/tcp` and `4001/udp` are separately published for IPFS
participation, not for Curio administration.

Read-only resolver and media routes may be public. Every mutation requires a
bearer token: `POST /keep`, `POST /seed`, `POST /store`, mutable favorites and
overrides, and `pin=1` actions on `/resolve` or `/wallet`. The installer writes
a random `CURIO_CURATOR_TOKEN` to
`$XDG_CONFIG_HOME/curio/curio.env`; protect that file (mode 0600) and send it
as `Authorization: Bearer <token>`. An empty resolver token disables mutations.

Terminate TLS at a trusted front door or proxy. Direct requests derive returned
Curio URLs from their request origin, and forwarded headers are ignored by
default. `CURIO_PUBLIC_BASE_URL` explicitly overrides the request origin for a
proxy deployment or non-request MCP invocation. Otherwise, set
`CURIO_TRUSTED_PROXY_CIDRS` only to the IP/CIDR ranges of immediate trusted
proxies to enable forwarded origin handling. Curio then accepts only a complete,
valid RFC `Forwarded` origin or `X-Forwarded-Proto` with
`X-Forwarded-Host`, and only when the connecting peer is in that allowlist.
Never allowlist client ranges: a trusted proxy must strip or replace inbound
forwarded headers before sending its own.

Curio follows user- and metadata-supplied HTTP URLs. It resolves DNS before
connecting, rejects prohibited address ranges, pins the connection to a checked
address, and validates every redirect target. Body-size, redirect, concurrency,
and timeout limits apply. Treat this as defense in depth: run the resolver with
only the outbound access appropriate to its curator workload and keep Docker,
Curio, and its dependencies updated.

Kept IPFS content is pinned and Kubo is configured to participate. Kept
Arweave content uses an isolated retained r81 Core and remains served through
Curio's AR.IO path. Neither is proof of public reachability: `/healthz` reports
Kubo evidence conservatively and AR.IO reachability as unknown where r81 has no
probe. AR.IO retention is not an r81 pin API and does not create new Arweave
replicas.

The appliance contacts public IPFS peers/gateways, AR.IO upstreams, Blockscout,
BENS, and TzKT as needed. Their availability, privacy practices, and returned
content are outside Curio's control.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting:

<https://github.com/dmichael/curio/security/advisories/new>

Include the affected revision, deployment topology, reproduction steps, and
impact. Do not open a public issue for an unpatched vulnerability.

## Supported versions

Until a tagged release is published, security fixes apply to the current `main`
branch. This policy will be revised when release branches exist.

# Security

Curio exposes one HTTP service on port 8090. Kubo and AR.IO HTTP interfaces stay
inside the Compose network. Kubo port 4001 is public only for IPFS peer traffic.

Read-only resolution and media routes may be public. These actions require the
curator bearer token:

- keep and pin requests;
- seed jobs;
- uploads;
- favorite changes;
- override changes.

The installer creates `CURIO_CURATOR_TOKEN` in
`~/.config/curio/curio.env` with file mode 0600. Protect that file. If the
resolver token is empty, mutations are disabled.

Use TLS when Curio is reachable over an untrusted network. Returned media URLs
normally use the request origin. `CURIO_PUBLIC_BASE_URL` can set a fixed public
origin. Forwarded origin headers are ignored unless the connecting proxy is
listed in `CURIO_TRUSTED_PROXY_CIDRS`; trusted proxies must replace
client-supplied forwarded headers.

Curio fetches URLs found in requests and metadata. It rejects local and private
network targets, checks DNS before connecting, checks redirects, and limits
body size, redirects, concurrency, and request time. Network policy outside the
container remains the operator's responsibility.

Curio contacts IPFS peers and gateways, Arweave nodes and gateways, Blockscout,
BENS, and TzKT. Their availability and privacy policies are outside Curio's
control.

## Reporting a vulnerability

Use GitHub private vulnerability reporting:

<https://github.com/dmichael/curio/security/advisories/new>

Include the affected revision, deployment details, reproduction steps, and
impact. Do not publish an unpatched vulnerability in an issue.

## Supported versions

Until the first release, security fixes apply to the current `main` branch.

# Security

Curio is designed for a trusted household or studio network. It has no user
authentication or per-action authorization. Any client that can reach port 8090
can submit and upload media, start seed jobs, and modify favorites and overrides.
Do not expose Curio directly to the public internet.

Only the resolver HTTP service is published on port 8090. Kubo and AR.IO HTTP
interfaces stay inside the Compose network. Kubo port 4001 is public only for
IPFS peer traffic.

Returned media URLs normally use the request origin. `CURIO_PUBLIC_BASE_URL` can
set a fixed public origin. Forwarded origin headers are ignored unless the
connecting proxy is listed in `CURIO_TRUSTED_PROXY_CIDRS`; trusted proxies must
replace client-supplied forwarded headers.

Trusted callers can still submit references containing hostile remote metadata.
Curio therefore rejects local and private network fetch targets, validates DNS
before connecting, validates redirects, and limits response size, redirect
count, concurrency, and request time. Network policy outside the container
remains the operator's responsibility.

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

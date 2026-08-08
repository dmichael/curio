# Security

## Deployment boundary

Curio 0.1 is intended for a trusted private network. It does not provide
accounts, authentication, authorization, TLS, or CSRF protection.

Anyone who can reach the resolver on port 8090 can start seed jobs, pin content,
upload files, change favorites, and edit the override registry. The IPFS and
Arweave gateways are also reachable on ports 8080 and 3000. Keep all three
ports behind a host or network firewall and do not forward them from an
internet-facing router.

Curio follows user- and metadata-supplied HTTP URLs. It rejects literal private,
loopback, and link-local addresses, but it does not currently pin DNS results or
revalidate every redirect target. Treat its outbound HTTP access as trusted-LAN
functionality, not as an isolation boundary. Do not run Curio on a network where
untrusted users can submit requests or control NFT metadata without accepting
that risk.

The appliance also contacts public IPFS, Arweave, Blockscout, BENS, TzKT, and
configured recovery gateways. Their availability and privacy policies are
outside Curio's control.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository:

<https://github.com/dmichael/curio/security/advisories/new>

Include the affected revision, deployment topology, reproduction steps, and
impact. Do not open a public issue for an unpatched vulnerability.

## Supported versions

Until the first tagged release, only the current `main` branch receives
security fixes. This policy will be updated when release branches exist.

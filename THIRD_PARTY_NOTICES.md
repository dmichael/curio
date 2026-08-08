# Third-party components

Curio's MIT license covers the source in this repository. The appliance pulls
and runs third-party container images under their own licenses.

The pinned components include:

| Component | Upstream | License information |
|---|---|---|
| Kubo | <https://github.com/ipfs/kubo> | MIT / Apache-2.0; see the upstream repository |
| AR.IO Core | <https://github.com/ar-io/ar-io-node> | AGPL-3.0-or-later; see the upstream repository |
| Python | <https://www.python.org/> | Python Software Foundation License |

Python packages installed into the resolver image retain their own license and
notice files. Exact package and image versions are recorded in
`resolver/constraints.txt`, `resolver/Dockerfile`, and
`appliance/compose.yaml`.

This file is a practical inventory, not legal advice. Review the linked terms
before redistributing a bundled appliance or container-image mirror.

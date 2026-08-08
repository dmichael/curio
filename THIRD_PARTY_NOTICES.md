# Third-party components

Curio's MIT license covers the source in this repository. The appliance pulls
and runs third-party container images under their own licenses.

The pinned components include:

| Component | Upstream | License information |
|---|---|---|
| Kubo | <https://github.com/ipfs/kubo> | MIT / Apache-2.0; see the upstream repository |
| AR.IO core and observer | <https://github.com/ar-io/ar-io-node> | AGPL-3.0-or-later; see the upstream repository |
| AR.IO Envoy image | <https://github.com/ar-io/ar-io-node> and <https://www.envoyproxy.io/> | Upstream AR.IO notices and Envoy's Apache-2.0 license apply |
| Redis 7.4 | <https://github.com/redis/redis> | Redis Source Available License 2.0 or SSPL 1.0; Redis 7.4 is not under the earlier BSD license |
| Python | <https://www.python.org/> | Python Software Foundation License |

Python packages installed into the resolver image retain their own license and
notice files. Exact package and image versions are recorded in
`resolver/constraints.txt`, `resolver/Dockerfile`, and
`appliance/compose.yaml`.

This file is a practical inventory, not legal advice. Review the linked terms
before redistributing a bundled appliance or container-image mirror.

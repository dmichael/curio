#!/usr/bin/env bash
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)

# These assertions deliberately do not need Docker; a disposable-host Compose
# run remains an operator validation because it pulls substantial upstream images.
grep -q 'CURIO_APP_ROOT' "$ROOT/appliance/install.sh"
grep -q 'CURIO_CURATOR_TOKEN' "$ROOT/appliance/install.sh"
legacy_name='CURIO_LAN''_ADDRESS'
privilege_tool='su''do'
for source in "$ROOT/appliance/compose.yaml" "$ROOT/appliance/install.sh" "$ROOT/install.sh"; do
    ! grep -E "$legacy_name|$privilege_tool" "$source"
done
! grep -q 'RESOLVER_IPFS_PUBLIC_BASE\|RESOLVER_ARWEAVE_PUBLIC_BASE' "$ROOT/appliance/compose.yaml"
grep -q 'RESOLVER_IPFS_INTERNAL' "$ROOT/appliance/compose.yaml"
grep -q '4001:4001' "$ROOT/appliance/compose.yaml"
grep -q 'update --check' "$ROOT/appliance/curio"
grep -q 'release tag.*does not match package version' "$ROOT/scripts/package-release.sh"

# Shell source and operator CLI syntax are portable sh.
sh -n "$ROOT/install.sh" "$ROOT/appliance/install.sh" "$ROOT/appliance/curio" "$ROOT/appliance/kubo-init.sh"
echo 'appliance tests passed'

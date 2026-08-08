#!/bin/sh
# Curio release bootstrap. Downloads a signed-off release archive, verifies its
# published SHA-256 checksum, then invokes the appliance installer from a file.
set -eu

REPOSITORY=${CURIO_GITHUB_REPOSITORY:-dmichael/curio}
VERSION=${CURIO_VERSION:-}
ASSET=curio-appliance.tar.gz
CHECKSUM_ASSET=curio-appliance.tar.gz.sha256

fail() {
    echo "curio installer: $*" >&2
    exit 1
}

[ "$(uname -s)" = Linux ] || fail "Curio can only be installed on Linux"
for command in curl tar sha256sum mktemp; do
    command -v "$command" >/dev/null 2>&1 || fail "$command is required"
done

if [ -n "$VERSION" ]; then
    case "$VERSION" in
        v[0-9]*.[0-9]*.[0-9]*) ;;
        *) fail "CURIO_VERSION must look like v0.1.0" ;;
    esac
    release_base="https://github.com/$REPOSITORY/releases/download/$VERSION"
else
    release_base="https://github.com/$REPOSITORY/releases/latest/download"
fi
release_base=${CURIO_RELEASE_BASE_URL:-$release_base}

tmp=$(mktemp -d /tmp/curio-install.XXXXXX)
trap 'rm -rf -- "$tmp"' EXIT HUP INT TERM

archive="$tmp/$ASSET"
checksum="$tmp/$CHECKSUM_ASSET"
echo "Downloading Curio from $release_base ..."
curl -fL --retry 3 --connect-timeout 15 -o "$archive" "$release_base/$ASSET"
curl -fL --retry 3 --connect-timeout 15 -o "$checksum" "$release_base/$CHECKSUM_ASSET"

expected=$(awk -v asset="$ASSET" '$2 == asset || $2 == "*" asset { print $1; exit }' "$checksum")
case "$expected" in
    ''|*[!0-9a-fA-F]*) fail "release checksum file is invalid" ;;
esac
[ "${#expected}" -eq 64 ] || fail "release checksum is not SHA-256"
actual=$(sha256sum "$archive" | awk '{print $1}')
[ "$actual" = "$expected" ] || fail "release archive checksum mismatch"

tar -xzf "$archive" -C "$tmp"
installer="$tmp/curio/appliance/install.sh"
project_file="$tmp/curio/resolver/pyproject.toml"
[ -f "$installer" ] || fail "release archive does not contain appliance/install.sh"
[ -f "$project_file" ] || fail "release archive does not contain resolver/pyproject.toml"
chmod 0755 "$installer"

project_version=$(awk -F '"' '/^version = / { print $2; exit }' "$project_file")
[ -n "$project_version" ] || fail "release archive has no project version"
release_tag="v$project_version"
if [ -n "$VERSION" ] && [ "$VERSION" != "$release_tag" ]; then
    fail "requested $VERSION but the archive contains $release_tag"
fi

if [ "$(id -u)" -eq 0 ]; then
    CURIO_RELEASE_VERSION="$release_tag" \
    CURIO_RELEASE_SHA256="$actual" \
    CURIO_RELEASE_SOURCE="$release_base" \
        "$installer"
else
    command -v sudo >/dev/null 2>&1 || fail "sudo is required to install Curio"
    sudo env \
        CURIO_LAN_ADDRESS="${CURIO_LAN_ADDRESS:-}" \
        CURIO_DATA_ROOT="${CURIO_DATA_ROOT:-}" \
        CURIO_IPFS_STORAGE_MAX="${CURIO_IPFS_STORAGE_MAX:-}" \
        CURIO_REDIS_MAX_MEMORY="${CURIO_REDIS_MAX_MEMORY:-}" \
        CURIO_AR_IO_CACHE_CLEANUP_THRESHOLD="${CURIO_AR_IO_CACHE_CLEANUP_THRESHOLD:-}" \
        CURIO_HEALTH_TIMEOUT="${CURIO_HEALTH_TIMEOUT:-}" \
        CURIO_RELEASE_VERSION="$release_tag" \
        CURIO_RELEASE_SHA256="$actual" \
        CURIO_RELEASE_SOURCE="$release_base" \
        "$installer"
fi

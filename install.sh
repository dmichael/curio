#!/bin/sh
# Verified Curio release bootstrap. The archive checksum is verified before
# any release-supplied installer runs; installation itself is per-user.
set -eu
REPOSITORY=${CURIO_GITHUB_REPOSITORY:-dmichael/curio}
VERSION=${CURIO_VERSION:-}
ASSET=curio-appliance.tar.gz
CHECKSUM_ASSET=$ASSET.sha256
fail() { echo "curio installer: $*" >&2; exit 1; }
valid_release_version() {
    case "$1" in v*) release_numbers=${1#v} ;; *) return 1 ;; esac
    case "$release_numbers" in ''|.*|*.|*..*|*[!0-9.]*) return 1 ;; esac
    old_ifs=$IFS; IFS=.; set -- $release_numbers; IFS=$old_ifs
    [ "$#" -eq 3 ] && [ -n "$1" ] && [ -n "$2" ] && [ -n "$3" ]
}
[ "$(uname -s)" = Linux ] || fail "Curio can only be installed on Linux"
for command in curl tar sha256sum mktemp; do command -v "$command" >/dev/null 2>&1 || fail "$command is required"; done
release_root=${CURIO_RELEASE_BASE_URL:-"https://github.com/$REPOSITORY/releases"}
release_root=${release_root%/}
if [ -z "$VERSION" ]; then
    VERSION=$(curl -fsSL --connect-timeout 10 --max-time 60 --retry 3 "$release_root/latest/download/VERSION" 2>/dev/null) \
        || fail "could not fetch latest VERSION"
    valid_release_version "$VERSION" || fail "latest release does not publish a valid VERSION"
fi
valid_release_version "$VERSION" || fail "CURIO_VERSION must look like vX.Y.Z"
base="$release_root/download/$VERSION"
tmp=$(mktemp -d "${TMPDIR:-/tmp}/curio-install.XXXXXX")
trap 'rm -rf -- "$tmp"' EXIT HUP INT TERM
curl -fL --connect-timeout 10 --max-time 600 --retry 3 -o "$tmp/$ASSET" "$base/$ASSET"
curl -fL --connect-timeout 10 --max-time 60 --retry 3 -o "$tmp/$CHECKSUM_ASSET" "$base/$CHECKSUM_ASSET"
expected=$(awk -v a="$ASSET" '$2 == a || $2 == "*" a {print $1; exit}' "$tmp/$CHECKSUM_ASSET")
case "$expected" in ??????*) ;; *) fail "invalid release checksum";; esac
[ "$(sha256sum "$tmp/$ASSET" | awk '{print $1}')" = "$expected" ] || fail "release archive checksum mismatch"
tar -xzf "$tmp/$ASSET" -C "$tmp"
project="$tmp/curio/resolver/pyproject.toml"; installer="$tmp/curio/appliance/install.sh"
[ -f "$project" ] && [ -f "$installer" ] || fail "release archive is incomplete"
package_version=$(awk -F '"' '/^version = / {print $2; exit}' "$project")
tag="v$package_version"
[ -n "$package_version" ] || fail "release archive has no package version"
[ -z "$VERSION" ] || [ "$VERSION" = "$tag" ] || fail "requested $VERSION but archive contains $tag"
CURIO_RELEASE_VERSION="$tag" "$installer"

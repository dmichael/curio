#!/bin/sh
set -eu

ROOT=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
VERSION=${1:-}
# Strict digits-only X.Y.Z, matching what the installed curio wrapper accepts:
# a looser tag (v1.0.0rc1) would publish a release every appliance rejects.
valid_release_version() {
    case "$1" in v*) numbers=${1#v} ;; *) return 1 ;; esac
    case "$numbers" in ''|.*|*.|*..*|*[!0-9.]*) return 1 ;; esac
    old_ifs=$IFS; IFS=.; set -- $numbers; IFS=$old_ifs
    [ "$#" -eq 3 ]
}
valid_release_version "$VERSION" \
    || { echo "usage: scripts/package-release.sh vX.Y.Z" >&2; exit 2; }

cd "$ROOT"
git rev-parse --verify "refs/tags/$VERSION" >/dev/null 2>&1 \
    || { echo "tag does not exist: $VERSION" >&2; exit 1; }
package_version=$(git show "$VERSION:resolver/pyproject.toml" | awk -F '"' '/^version = / {print $2; exit}')
[ -n "$package_version" ] \
    || { echo "tagged release has no package version: $VERSION" >&2; exit 1; }
[ "$VERSION" = "v$package_version" ] \
    || { echo "release tag $VERSION does not match tagged package version v$package_version" >&2; exit 1; }
image_version=$(git show "$VERSION:appliance/compose.yaml" | awk '/image: curio-resolver:/{sub(/^.*curio-resolver:/, ""); print; exit}')
[ "$image_version" = "$package_version" ] \
    || { echo "tagged resolver image version $image_version does not match package version $package_version" >&2; exit 1; }

mkdir -p dist
rm -f dist/curio-appliance.tar.gz dist/curio-appliance.tar.gz.sha256 dist/install.sh
git archive --format=tar.gz --prefix=curio/ \
    --output=dist/curio-appliance.tar.gz "$VERSION"
git show "$VERSION:install.sh" >dist/install.sh
chmod 0755 dist/install.sh
(
    cd dist
    sha256sum curio-appliance.tar.gz >curio-appliance.tar.gz.sha256
)
printf '%s\n' "$VERSION" >dist/VERSION

echo "Release assets written to $ROOT/dist"

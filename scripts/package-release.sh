#!/bin/sh
set -eu

ROOT=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
VERSION=${1:-}
case "$VERSION" in
    v[0-9]*.[0-9]*.[0-9]*) ;;
    *) echo "usage: scripts/package-release.sh vX.Y.Z" >&2; exit 2 ;;
esac

cd "$ROOT"
git rev-parse --verify "refs/tags/$VERSION" >/dev/null 2>&1 \
    || { echo "tag does not exist: $VERSION" >&2; exit 1; }
package_version=$(awk -F '"' '/^version = / {print $2; exit}' resolver/pyproject.toml)
[ "$VERSION" = "v$package_version" ] \
    || { echo "release tag $VERSION does not match package version v$package_version" >&2; exit 1; }

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

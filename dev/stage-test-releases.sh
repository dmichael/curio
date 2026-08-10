#!/usr/bin/env bash
# Build tagged release assets into a GitHub-compatible local release tree.
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
OUTPUT=${1:-}
LATEST=${2:-}
shift $(( $# >= 2 ? 2 : $# ))
fail(){ echo "stage-test-releases: $*" >&2; exit 1; }
[[ -n $OUTPUT && -n $LATEST && $# -gt 0 ]] || {
  echo "usage: $0 OUTPUT_DIR LATEST_TAG TAG [TAG ...]" >&2
  exit 2
}
[[ $LATEST == v*.*.* ]] || fail 'latest tag must look like vX.Y.Z'
[[ ! -e $OUTPUT ]] || fail "output already exists: $OUTPUT"

found_latest=false
for tag in "$@"; do
  [[ $tag == v*.*.* ]] || fail "invalid release tag: $tag"
  [[ $tag != "$LATEST" ]] || found_latest=true
  "$ROOT/scripts/package-release.sh" "$tag"
  target="$OUTPUT/download/$tag"
  mkdir -p "$target"
  cp "$ROOT/dist/install.sh" \
     "$ROOT/dist/curio-appliance.tar.gz" \
     "$ROOT/dist/curio-appliance.tar.gz.sha256" \
     "$ROOT/dist/VERSION" \
     "$target/"
done
$found_latest || fail "latest tag was not staged: $LATEST"
mkdir -p "$OUTPUT/latest/download"
cp "$OUTPUT/download/$LATEST/"* "$OUTPUT/latest/download/"
printf 'Release root staged at %s (latest: %s)\n' "$OUTPUT" "$LATEST"

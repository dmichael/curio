#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
fail(){ echo "check-image-platforms: $*" >&2; exit 1; }
command -v docker >/dev/null || fail 'docker is required'
docker buildx version >/dev/null 2>&1 || fail 'docker buildx is required'

# Every Compose image except the locally built resolver, plus the resolver's
# base image. Derived from the sources so a newly pinned image cannot be
# silently skipped. No mapfile: macOS system bash is 3.2.
images=()
while IFS= read -r image; do images+=("$image"); done < <(
  awk '/^[[:space:]]+image: /{print $2}' "$ROOT/appliance/compose.yaml" | grep -v '^curio-resolver:'
  awk '$1 == "FROM" {print $2; exit}' "$ROOT/resolver/Dockerfile"
)
[[ ${#images[@]} -ge 3 ]] || fail 'could not identify the pinned base images'

for image in "${images[@]}"; do
  manifest=$(docker buildx imagetools inspect "$image") || fail "could not inspect $image"
  grep -Eq 'Platform:[[:space:]]+linux/amd64([[:space:]]|$)' <<<"$manifest" \
    || fail "$image has no linux/amd64 manifest"
  grep -Eq 'Platform:[[:space:]]+linux/arm64(/v8)?([[:space:]]|$)' <<<"$manifest" \
    || fail "$image has no linux/arm64 manifest"
  echo "$image: linux/amd64 and linux/arm64"
done

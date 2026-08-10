#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
fail(){ echo "check-image-platforms: $*" >&2; exit 1; }
command -v docker >/dev/null || fail 'docker is required'
docker buildx version >/dev/null 2>&1 || fail 'docker buildx is required'

mapfile -t images < <(
  awk '/^[[:space:]]+image: (ipfs\/kubo|ghcr\.io\/ar-io\/ar-io-core):/{print $2}' "$ROOT/appliance/compose.yaml"
  awk '$1 == "FROM" {print $2; exit}' "$ROOT/resolver/Dockerfile"
)
[[ ${#images[@]} -eq 3 ]] || fail 'could not identify all three pinned base images'

for image in "${images[@]}"; do
  manifest=$(docker buildx imagetools inspect "$image") || fail "could not inspect $image"
  grep -Eq 'Platform:[[:space:]]+linux/amd64([[:space:]]|$)' <<<"$manifest" \
    || fail "$image has no linux/amd64 manifest"
  grep -Eq 'Platform:[[:space:]]+linux/arm64(/v8)?([[:space:]]|$)' <<<"$manifest" \
    || fail "$image has no linux/arm64 manifest"
  echo "$image: linux/amd64 and linux/arm64"
done

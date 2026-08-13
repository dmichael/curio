#!/usr/bin/env bash
# Destructive only to a disposable VM, but deliberately no-sudo: it validates
# the installed user's XDG roots, persistent mounts, restart/failure/reboot.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
MODE=${1:-}
# Public qualified fixture. Override only to exercise another known object.
ARWEAVE_TXID=${CURIO_TEST_ARWEAVE_TXID:-18VeoHbl4kVO0wPGcneapz8MT0y8CeTwBbR13UOlImo}
ARWEAVE_SHA256=${CURIO_TEST_ARWEAVE_SHA256:-0ee462e8e0f5c2fb02cd77f45e03cc67c34dcc6ba0e92feacb5e1fe9a7241e18}
EVIDENCE_DIR=${CURIO_TEST_EVIDENCE_DIR:-"${XDG_STATE_HOME:-$HOME/.local/state}/curio-appliance-test"}
CONFIG_HOME=${XDG_CONFIG_HOME:-"$HOME/.config"}
DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
ENV_FILE=${CURIO_ENV_FILE:-"$CONFIG_HOME/curio/curio.env"}
APP_ROOT=${CURIO_APP_ROOT:-"$DATA_HOME/curio/app"}
DATA_ROOT=${CURIO_DATA_ROOT:-"$DATA_HOME/curio/state"}
RELEASE_BASE_URL=${CURIO_TEST_RELEASE_BASE_URL:-}
INSTALL_VERSION=${CURIO_TEST_INSTALL_VERSION:-}
UPDATE_VERSION=${CURIO_TEST_UPDATE_VERSION:-}
LATEST_VERSION=${CURIO_TEST_LATEST_VERSION:-}
REJECT_VERSION=${CURIO_TEST_REJECT_VERSION:-}
FAILED_UPDATE_VERSION=${CURIO_TEST_FAILED_UPDATE_VERSION:-}
fail(){ echo "test-appliance: $*" >&2; exit 1; }
usage(){ echo "usage: $0 --disposable-vm|--after-reboot" >&2; }
[[ $MODE == --disposable-vm || $MODE == --after-reboot ]] || { usage; exit 2; }
[[ $(uname -s) == Linux ]] || fail 'run inside a Linux VM'
command -v systemd-detect-virt >/dev/null && systemd-detect-virt --quiet || fail 'refusing non-virtualized host'
for c in docker curl python3 sha256sum; do command -v "$c" >/dev/null || fail "$c is required"; done
docker compose version >/dev/null || fail 'docker compose plugin is required'
if [[ -n $RELEASE_BASE_URL ]]; then
  valid_version(){ [[ $1 =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; }
  valid_version "$INSTALL_VERSION" && valid_version "$UPDATE_VERSION" && valid_version "$LATEST_VERSION" || fail 'release qualification requires install, exact-update, and latest versions'
  [[ $INSTALL_VERSION != "$UPDATE_VERSION" && $INSTALL_VERSION != "$LATEST_VERSION" && $UPDATE_VERSION != "$LATEST_VERSION" ]] || fail 'install, exact-update, and latest versions must differ'
  for version in "$REJECT_VERSION" "$FAILED_UPDATE_VERSION"; do
    [[ -z $version ]] || valid_version "$version" || fail "invalid test release version: $version"
  done
  published_latest=$(curl -fsSL --connect-timeout 10 --max-time 60 --retry 3 "$RELEASE_BASE_URL/latest/download/VERSION") || fail 'latest VERSION fixture is unavailable'
  [[ $published_latest == "$LATEST_VERSION" ]] || fail "latest fixture publishes $published_latest, expected $LATEST_VERSION"
  for version in "$INSTALL_VERSION" "$UPDATE_VERSION" "$LATEST_VERSION" "$REJECT_VERSION" "$FAILED_UPDATE_VERSION"; do
    [[ -z $version ]] && continue
    for asset in install.sh curio-appliance.tar.gz curio-appliance.tar.gz.sha256; do
      curl -fsS --connect-timeout 10 --max-time 60 --retry 3 -o /dev/null "$RELEASE_BASE_URL/download/$version/$asset" || fail "fixture unavailable: $version/$asset"
    done
  done
elif [[ -n $INSTALL_VERSION || -n $UPDATE_VERSION || -n $LATEST_VERSION || -n $REJECT_VERSION || -n $FAILED_UPDATE_VERSION ]]; then
  fail 'release test versions require CURIO_TEST_RELEASE_BASE_URL'
fi
mkdir -p "$EVIDENCE_DIR"
compose(){ docker compose --project-name curio --env-file "$ENV_FILE" --file "$APP_ROOT/current/compose.yaml" "$@"; }
health(){ "$HOME/.local/bin/curio" health >"$EVIDENCE_DIR/health.latest" 2>&1; }
wait_healthy(){
  local stable=0
  for _ in $(seq 1 120); do
    if health; then ((stable += 1)); ((stable >= 3)) && return; else stable=0; fi
    sleep 5
  done
  cat "$EVIDENCE_DIR/health.latest" >&2
  fail 'Curio did not remain healthy'
}
install_release(){
  local version=$1 bootstrap="$EVIDENCE_DIR/install-$1.sh"
  curl -fsSL --connect-timeout 10 --max-time 60 --retry 3 "$RELEASE_BASE_URL/download/$version/install.sh" -o "$bootstrap"
  CURIO_RELEASE_BASE_URL="$RELEASE_BASE_URL" CURIO_VERSION="$version" sh "$bootstrap"
}
release_curio(){ CURIO_RELEASE_BASE_URL="$RELEASE_BASE_URL" "$HOME/.local/bin/curio" "$@"; }
fetch_arweave_fixture(){
  local txid=$1 output=$2 headers=$3 code
  for _ in $(seq 1 12); do
    code=$(curl -sS -L --max-time 600 -D "$headers" -o "$output" -w '%{http_code}' "http://127.0.0.1:8090/arweave/$txid" || true)
    [[ $code == 200 ]] && return
    sleep 5
  done
  fail "AR.IO fixture failed (HTTP ${code:-none})"
}
fetch_arweave_cache_hit(){
  local txid=$1 output=$2 headers=$3
  for _ in $(seq 1 12); do
    fetch_arweave_fixture "$txid" "$output" "$headers"
    grep -Eiq '^x-cache:[[:space:]]*HIT' "$headers" && return
    sleep 5
  done
  fail 'AR.IO fixture did not become a cache hit'
}
verify(){
  local ref
  ref=$(<"$EVIDENCE_DIR/media-ref")
  curl -fsS http://127.0.0.1:8090/healthz >/dev/null
  [[ $(curl -fsSL --get --data-urlencode "ref=$ref" http://127.0.0.1:8090/resolve) == "$(<"$EVIDENCE_DIR/payload")" ]] || fail 'static media lost'
  curl -fsS http://127.0.0.1:8090/favorites | grep -F 'bafytest' >/dev/null || fail 'favorite lost'
  [[ $(sha256sum "$ENV_FILE" | awk '{print $1}') == $(<"$EVIDENCE_DIR/env.sha") ]] || fail 'configuration changed'
  [[ $(sha256sum "$DATA_ROOT/ar-io/start-height.env" | awk '{print $1}') == $(<"$EVIDENCE_DIR/height.sha") ]] || fail 'start height changed'
  [[ -z $(find "$DATA_ROOT" -xdev \( ! -user "$(id -u)" -o ! -group "$(id -g)" \) -print -quit) ]] || fail 'container created root-owned state'
  if [[ -f $EVIDENCE_DIR/arweave.txid ]]; then
    fetch_arweave_cache_hit "$(<"$EVIDENCE_DIR/arweave.txid")" "$EVIDENCE_DIR/arweave.verify" "$EVIDENCE_DIR/arweave.headers"
    [[ $(sha256sum "$EVIDENCE_DIR/arweave.verify" | awk '{print $1}') == $(<"$EVIDENCE_DIR/arweave.sha") ]] || fail 'AR.IO fixture bytes changed'
  fi
  compose ps --all --quiet | xargs -r docker inspect >"$EVIDENCE_DIR/containers.json"
  python3 - "$EVIDENCE_DIR/containers.json" <<'PY'
import json, pathlib, sys
for c in json.loads(pathlib.Path(sys.argv[1]).read_text() or '[]'):
    name=c['Config']['Labels']['com.docker.compose.service']; ports=c['HostConfig'].get('PortBindings') or {}
    if name == 'ar-io-core': assert not ports, (name,ports)
    if name=='kubo': assert set(ports) <= {'4001/tcp','4001/udp'}, ports
PY
}
if [[ $MODE == --after-reboot ]]; then
  [[ -f $EVIDENCE_DIR/media-ref ]] || fail 'first-phase evidence is absent'
  wait_healthy; verify
  cp "$EVIDENCE_DIR/health.latest" "$EVIDENCE_DIR/health.after-reboot"
  echo "post-reboot checks passed; evidence: $EVIDENCE_DIR"; exit
fi
if [[ -n $RELEASE_BASE_URL ]]; then
  if [[ -n $REJECT_VERSION ]]; then
    if install_release "$REJECT_VERSION" >"$EVIDENCE_DIR/rejected-install.log" 2>&1; then
      fail 'release with an invalid checksum was accepted'
    fi
    grep -F 'release archive checksum mismatch' "$EVIDENCE_DIR/rejected-install.log" >/dev/null || fail 'reject fixture did not reach checksum verification'
    [[ ! -e $APP_ROOT/current ]] || fail 'rejected release changed the active release'
  fi
  install_release "$INSTALL_VERSION"
else
  "$ROOT/appliance/install.sh"
fi
wait_healthy
readlink "$APP_ROOT/current" >"$EVIDENCE_DIR/release.before-rerun"
[[ $ARWEAVE_TXID =~ ^[A-Za-z0-9_-]{43}$ && $ARWEAVE_SHA256 =~ ^[a-fA-F0-9]{64}$ ]] || fail 'set both valid CURIO_TEST_ARWEAVE_TXID and CURIO_TEST_ARWEAVE_SHA256'
fetch_arweave_fixture "$ARWEAVE_TXID" "$EVIDENCE_DIR/arweave.initial" "$EVIDENCE_DIR/arweave.initial.headers"
[[ $(sha256sum "$EVIDENCE_DIR/arweave.initial" | awk '{print $1}') == "${ARWEAVE_SHA256,,}" ]] || fail 'AR.IO fixture checksum mismatch'
# A second read proves the same persistent Core now serves a native cache hit.
fetch_arweave_cache_hit "$ARWEAVE_TXID" "$EVIDENCE_DIR/arweave.cached" "$EVIDENCE_DIR/arweave.cached.headers"
[[ $(sha256sum "$EVIDENCE_DIR/arweave.cached" | awk '{print $1}') == "${ARWEAVE_SHA256,,}" ]] || fail 'cached AR.IO fixture checksum mismatch'
# POST resolution stores through the same Core; it is not a claim of
# replication into the Arweave network.
resolution=$(curl -fsS -X POST --get --data-urlencode "ref=ar://$ARWEAVE_TXID" http://127.0.0.1:8090/resolve)
printf %s "$resolution" | python3 -c 'import json,sys; assert json.load(sys.stdin)["status"] == "ready"'
printf '%s\n' "$ARWEAVE_TXID" >"$EVIDENCE_DIR/arweave.txid"
printf '%s\n' "${ARWEAVE_SHA256,,}" >"$EVIDENCE_DIR/arweave.sha"
printf 'curio-persistence-%s\n' "$(date +%s)" >"$EVIDENCE_DIR/payload"
stored=$(curl -fsS -F "file=@$EVIDENCE_DIR/payload" http://127.0.0.1:8090/resolve)
printf %s "$stored" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ref"])' >"$EVIDENCE_DIR/media-ref"
# Record an IPFS favorite independently from the uploaded-media persistence check.
curl -fsS -X POST --get --data-urlencode 'ref=ipfs://bafytest/art.png' http://127.0.0.1:8090/favorites >/dev/null 2>&1 || true
sha256sum "$ENV_FILE" | awk '{print $1}' >"$EVIDENCE_DIR/env.sha"
sha256sum "$DATA_ROOT/ar-io/start-height.env" | awk '{print $1}' >"$EVIDENCE_DIR/height.sha"
if [[ -n $RELEASE_BASE_URL ]]; then
  release_curio update --check >"$EVIDENCE_DIR/update-check"
  grep -F "installed ${INSTALL_VERSION#v}; latest $LATEST_VERSION" "$EVIDENCE_DIR/update-check" >/dev/null || fail 'update check returned unexpected versions'
  release_curio update --version "$UPDATE_VERSION"
  wait_healthy
  [[ $(release_curio version) == "${UPDATE_VERSION#v}" ]] || fail 'exact-version update installed the wrong version'
  [[ $(readlink "$APP_ROOT/current") != $(<"$EVIDENCE_DIR/release.before-rerun") ]] || fail 'exact-version update did not replace release pointer'
  verify
  readlink "$APP_ROOT/current" >"$EVIDENCE_DIR/release.before-latest-update"
  release_curio update
  wait_healthy
  [[ $(release_curio version) == "${LATEST_VERSION#v}" ]] || fail 'latest update installed the wrong version'
  [[ $(readlink "$APP_ROOT/current") != $(<"$EVIDENCE_DIR/release.before-latest-update") ]] || fail 'latest update did not replace release pointer'
  verify
  if [[ -n $FAILED_UPDATE_VERSION ]]; then
    readlink "$APP_ROOT/current" >"$EVIDENCE_DIR/release.before-failed-update"
    if release_curio update --version "$FAILED_UPDATE_VERSION" >"$EVIDENCE_DIR/failed-update.log" 2>&1; then
      fail 'broken release update unexpectedly succeeded'
    fi
    grep -F 'Compose start or health check failed' "$EVIDENCE_DIR/failed-update.log" >/dev/null || fail 'failed-update fixture did not reach Compose startup'
    grep -F 'restoring previous release' "$EVIDENCE_DIR/failed-update.log" >/dev/null || fail 'failed update did not exercise rollback'
    [[ $(readlink "$APP_ROOT/current") == $(<"$EVIDENCE_DIR/release.before-failed-update") ]] || fail 'failed update did not restore the active release'
    [[ $(release_curio version) == "${LATEST_VERSION#v}" ]] || fail 'failed update changed the installed version'
    wait_healthy; verify
  fi
else
  "$ROOT/appliance/install.sh"; wait_healthy
  [[ $(readlink "$APP_ROOT/current") != $(<"$EVIDENCE_DIR/release.before-rerun") ]] || fail 'rerun did not replace release pointer'
  verify
fi
compose up -d --force-recreate --no-build; wait_healthy; verify
compose stop ar-io-core
if health; then fail 'health succeeded with AR.IO Core stopped'; fi
compose start ar-io-core; wait_healthy; verify
compose stop kubo
if health; then fail 'health succeeded with Kubo stopped'; fi
compose start kubo; wait_healthy; verify
cp "$EVIDENCE_DIR/health.latest" "$EVIDENCE_DIR/health.before-reboot"
echo "first phase passed; reboot VM then run: $0 --after-reboot"

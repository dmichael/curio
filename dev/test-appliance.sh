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
fail(){ echo "test-appliance: $*" >&2; exit 1; }
usage(){ echo "usage: $0 --disposable-vm|--after-reboot" >&2; }
[[ $MODE == --disposable-vm || $MODE == --after-reboot ]] || { usage; exit 2; }
[[ $(uname -s) == Linux ]] || fail 'run inside a Linux VM'
command -v systemd-detect-virt >/dev/null && systemd-detect-virt --quiet || fail 'refusing non-virtualized host'
for c in docker curl python3 sha256sum; do command -v "$c" >/dev/null || fail "$c is required"; done
docker compose version >/dev/null || fail 'docker compose plugin is required'
mkdir -p "$EVIDENCE_DIR"
compose(){ docker compose --project-name curio --env-file "$ENV_FILE" --file "$APP_ROOT/current/compose.yaml" "$@"; }
health(){ "$HOME/.local/bin/curio" health >"$EVIDENCE_DIR/health.latest" 2>&1; }
wait_healthy(){ for _ in $(seq 1 120); do health && return; sleep 5; done; cat "$EVIDENCE_DIR/health.latest" >&2; fail 'Curio did not become healthy'; }
fetch_arweave_fixture(){
  local txid=$1 output=$2 headers=$3 code
  code=$(curl -sS -L --max-time 600 -D "$headers" -o "$output" -w '%{http_code}' "http://127.0.0.1:8090/arweave/$txid" || true)
  [[ $code == 200 ]] || fail "AR.IO fixture failed (HTTP ${code:-none})"
}
verify(){
  local id token
  token=$(awk -F= '$1=="CURIO_CURATOR_TOKEN"{print $2}' "$ENV_FILE")
  id=$(<"$EVIDENCE_DIR/media-id")
  curl -fsS http://127.0.0.1:8090/healthz >/dev/null
  [[ $(curl -fsS "http://127.0.0.1:8090/media/$id") == "$(<"$EVIDENCE_DIR/payload")" ]] || fail 'static media lost'
  curl -fsS http://127.0.0.1:8090/favorites | grep -F 'bafytest' >/dev/null || fail 'favorite lost'
  [[ $(sha256sum "$ENV_FILE" | awk '{print $1}') == $(<"$EVIDENCE_DIR/env.sha") ]] || fail 'configuration changed'
  [[ $(sha256sum "$DATA_ROOT/ar-io/start-height.env" | awk '{print $1}') == $(<"$EVIDENCE_DIR/height.sha") ]] || fail 'start height changed'
  [[ -z $(find "$DATA_ROOT" -xdev \( ! -user "$(id -u)" -o ! -group "$(id -g)" \) -print -quit) ]] || fail 'container created root-owned state'
  if [[ -f $EVIDENCE_DIR/arweave.txid ]]; then
    fetch_arweave_fixture "$(<"$EVIDENCE_DIR/arweave.txid")" "$EVIDENCE_DIR/arweave.verify" "$EVIDENCE_DIR/arweave.headers"
    [[ $(sha256sum "$EVIDENCE_DIR/arweave.verify" | awk '{print $1}') == $(<"$EVIDENCE_DIR/arweave.sha") ]] || fail 'AR.IO fixture bytes changed'
    grep -Eiq '^x-cache:[[:space:]]*HIT' "$EVIDENCE_DIR/arweave.headers" || fail 'AR.IO fixture was not cached'
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
  [[ -f $EVIDENCE_DIR/media-id ]] || fail 'first-phase evidence is absent'
  wait_healthy; verify
  cp "$EVIDENCE_DIR/health.latest" "$EVIDENCE_DIR/health.after-reboot"
  echo "post-reboot checks passed; evidence: $EVIDENCE_DIR"; exit
fi
"$ROOT/appliance/install.sh"
wait_healthy
readlink "$APP_ROOT/current" >"$EVIDENCE_DIR/release.before-rerun"
token=$(awk -F= '$1=="CURIO_CURATOR_TOKEN"{print $2}' "$ENV_FILE")
[[ $ARWEAVE_TXID =~ ^[A-Za-z0-9_-]{43}$ && $ARWEAVE_SHA256 =~ ^[a-fA-F0-9]{64}$ ]] || fail 'set both valid CURIO_TEST_ARWEAVE_TXID and CURIO_TEST_ARWEAVE_SHA256'
fetch_arweave_fixture "$ARWEAVE_TXID" "$EVIDENCE_DIR/arweave.initial" "$EVIDENCE_DIR/arweave.initial.headers"
[[ $(sha256sum "$EVIDENCE_DIR/arweave.initial" | awk '{print $1}') == "${ARWEAVE_SHA256,,}" ]] || fail 'AR.IO fixture checksum mismatch'
# A second read proves the same persistent Core now serves a native cache hit.
fetch_arweave_fixture "$ARWEAVE_TXID" "$EVIDENCE_DIR/arweave.cached" "$EVIDENCE_DIR/arweave.cached.headers"
[[ $(sha256sum "$EVIDENCE_DIR/arweave.cached" | awk '{print $1}') == "${ARWEAVE_SHA256,,}" ]] || fail 'cached AR.IO fixture checksum mismatch'
grep -Eiq '^x-cache:[[:space:]]*HIT' "$EVIDENCE_DIR/arweave.cached.headers" || fail 'second AR.IO fetch was not a native cache hit'
# Keep is eager same-Core fetch and verification, not movement between tiers
# or a claim of replication into the Arweave network.
keep=$(curl -fsS -X POST -H "Authorization: Bearer $token" --get --data-urlencode "ref=ar://$ARWEAVE_TXID" http://127.0.0.1:8090/keep)
printf %s "$keep" | python3 -c 'import json,sys; assert json.load(sys.stdin)["keep_state"] == "kept"'
printf '%s\n' "$ARWEAVE_TXID" >"$EVIDENCE_DIR/arweave.txid"
printf '%s\n' "${ARWEAVE_SHA256,,}" >"$EVIDENCE_DIR/arweave.sha"
printf 'curio-persistence-%s\n' "$(date +%s)" >"$EVIDENCE_DIR/payload"
store=$(curl -fsS -H "Authorization: Bearer $token" -F "file=@$EVIDENCE_DIR/payload" http://127.0.0.1:8090/store)
printf %s "$store" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' >"$EVIDENCE_DIR/media-id"
id=$(<"$EVIDENCE_DIR/media-id")
curl -fsS -X POST -H "Authorization: Bearer $token" --get --data-urlencode "ref=http://127.0.0.1:8090/media/$id" http://127.0.0.1:8090/favorites >/dev/null || true
# Direct localhost HTTP is intentionally rejected by SSRF protection; record
# an IPFS favorite instead, while media persistence is checked independently.
curl -fsS -X POST -H "Authorization: Bearer $token" --get --data-urlencode 'ref=ipfs://bafytest/art.png' http://127.0.0.1:8090/favorites >/dev/null
sha256sum "$ENV_FILE" | awk '{print $1}' >"$EVIDENCE_DIR/env.sha"
sha256sum "$DATA_ROOT/ar-io/start-height.env" | awk '{print $1}' >"$EVIDENCE_DIR/height.sha"
"$ROOT/appliance/install.sh"; wait_healthy
[[ $(readlink "$APP_ROOT/current") != $(<"$EVIDENCE_DIR/release.before-rerun") ]] || fail 'rerun did not replace release pointer'
verify
compose up -d --force-recreate --no-build; wait_healthy; verify
compose stop ar-io-core
if health; then fail 'health succeeded with AR.IO Core stopped'; fi
compose start ar-io-core; wait_healthy; verify
compose stop kubo
if health; then fail 'health succeeded with Kubo stopped'; fi
compose start kubo; wait_healthy; verify
cp "$EVIDENCE_DIR/health.latest" "$EVIDENCE_DIR/health.before-reboot"
echo "first phase passed; reboot VM then run: $0 --after-reboot"

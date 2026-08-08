#!/usr/bin/env bash
# Destructive only to a disposable VM, but deliberately no-sudo: it validates
# the installed user's XDG roots, persistent mounts, restart/failure/reboot.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
MODE=${1:-}
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
verify(){
  local id token
  token=$(awk -F= '$1=="CURIO_CURATOR_TOKEN"{print $2}' "$ENV_FILE")
  id=$(<"$EVIDENCE_DIR/media-id")
  curl -fsS http://127.0.0.1:8090/healthz >/dev/null
  [[ $(curl -fsS "http://127.0.0.1:8090/media/$id") == "$(<"$EVIDENCE_DIR/payload")" ]] || fail 'static media lost'
  curl -fsS http://127.0.0.1:8090/favorites | grep -F 'bafytest' >/dev/null || fail 'favorite lost'
  [[ $(sha256sum "$ENV_FILE" | awk '{print $1}') == $(<"$EVIDENCE_DIR/env.sha") ]] || fail 'configuration changed'
  [[ $(sha256sum "$DATA_ROOT/ar-io/start-height.env" | awk '{print $1}') == $(<"$EVIDENCE_DIR/height.sha") ]] || fail 'start height changed'
  compose ps --all --quiet | xargs -r docker inspect >"$EVIDENCE_DIR/containers.json"
  python3 - "$EVIDENCE_DIR/containers.json" <<'PY'
import json, pathlib, sys
for c in json.loads(pathlib.Path(sys.argv[1]).read_text() or '[]'):
    name=c['Config']['Labels']['com.docker.compose.service']; ports=c['HostConfig'].get('PortBindings') or {}
    if name in {'ar-io-core','ar-io-redis','ar-io-observer','ar-io-envoy'}: assert not ports, (name,ports)
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
printf 'curio-persistence-%s\n' "$(date +%s)" >"$EVIDENCE_DIR/payload"
token=$(awk -F= '$1=="CURIO_CURATOR_TOKEN"{print $2}' "$ENV_FILE")
store=$(curl -fsS -H "Authorization: Bearer $token" -F "file=@$EVIDENCE_DIR/payload" http://127.0.0.1:8090/store)
printf %s "$store" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' >"$EVIDENCE_DIR/media-id"
id=$(<"$EVIDENCE_DIR/media-id")
curl -fsS -X POST -H "Authorization: Bearer $token" --get --data-urlencode "ref=http://127.0.0.1:8090/media/$id" http://127.0.0.1:8090/favorites >/dev/null || true
# Direct localhost HTTP is intentionally rejected by SSRF protection; record
# an IPFS favorite instead, while media persistence is checked independently.
curl -fsS -X POST -H "Authorization: Bearer $token" --get --data-urlencode 'ref=ipfs://bafytest/art.png' http://127.0.0.1:8090/favorites >/dev/null
sha256sum "$ENV_FILE" | awk '{print $1}' >"$EVIDENCE_DIR/env.sha"
sha256sum "$DATA_ROOT/ar-io/start-height.env" | awk '{print $1}' >"$EVIDENCE_DIR/height.sha"
"$ROOT/appliance/install.sh"; wait_healthy; verify
compose up -d --force-recreate --no-build; wait_healthy; verify
compose stop kubo
if health; then fail 'health succeeded with Kubo stopped'; fi
compose start kubo; wait_healthy; verify
cp "$EVIDENCE_DIR/health.latest" "$EVIDENCE_DIR/health.before-reboot"
echo "first phase passed; reboot VM then run: $0 --after-reboot"

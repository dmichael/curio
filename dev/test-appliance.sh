#!/usr/bin/env bash
# Destructive only to a disposable VM. This installs Curio in its real Linux
# paths, recreates containers, and deliberately stops a dependency.
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
EVIDENCE_DIR=${CURIO_TEST_EVIDENCE_DIR:-/var/tmp/curio-appliance-test}
MODE=${1:-}
ARWEAVE_TXID=${CURIO_TEST_ARWEAVE_TXID:-}
ARWEAVE_SHA256=${CURIO_TEST_ARWEAVE_SHA256:-}

fail() {
    echo "test-appliance: $*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage:
  sudo-free-shell$ dev/test-appliance.sh --disposable-vm
  sudo-free-shell$ dev/test-appliance.sh --after-reboot

The first command installs Curio and changes containers. It refuses to run when
Linux does not report a virtualized environment. Run --after-reboot in the same
VM after the first phase and a VM reboot.
EOF
}

[[ $MODE == --disposable-vm || $MODE == --after-reboot ]] || { usage >&2; exit 2; }
[[ $(uname -s) == Linux ]] || fail "run this test inside a Linux VM"
command -v systemd-detect-virt >/dev/null 2>&1 || fail "systemd-detect-virt is required"
systemd-detect-virt --quiet || fail "refusing to modify a non-virtualized host"
for command in curl docker python3 sha256sum sudo; do
    command -v "$command" >/dev/null 2>&1 || fail "$command is required in the sandbox"
done
docker compose version >/dev/null 2>&1 || fail "docker compose plugin is required"
if [[ -n $ARWEAVE_TXID || -n $ARWEAVE_SHA256 ]]; then
    [[ $ARWEAVE_TXID =~ ^[A-Za-z0-9_-]{43}$ ]] \
        || fail "CURIO_TEST_ARWEAVE_TXID must be a 43-character transaction ID"
    [[ $ARWEAVE_SHA256 =~ ^[a-fA-F0-9]{64}$ ]] \
        || fail "CURIO_TEST_ARWEAVE_SHA256 must be a 64-character checksum"
fi

compose() {
    sudo docker compose --project-name curio \
        --env-file /etc/curio/curio.env \
        --file /opt/curio/compose.yaml "$@"
}

wait_healthy() {
    local attempt
    for attempt in $(seq 1 120); do
        if sudo curio health >"$EVIDENCE_DIR/health.latest" 2>&1; then
            return 0
        fi
        sleep 5
    done
    cat "$EVIDENCE_DIR/health.latest" >&2
    fail "Curio did not become healthy"
}

fetch_arweave_fixture() {
    local txid=$1 output=$2 headers=$3 lan_address code attempt
    lan_address=$(<"$EVIDENCE_DIR/lan-address")
    for attempt in 1 2 3 4; do
        code=$(curl -sS -L --max-time 600 -D "$headers" -o "$output" \
            -w '%{http_code}' "http://$lan_address:3000/$txid" || true)
        [[ $code == 200 ]] && return 0
        sleep $((attempt * 20))
    done
    echo "AR.IO fixture failed after four attempts (last HTTP status: ${code:-none})" >&2
    return 1
}

check_published_ports() {
    compose ps --all --quiet | xargs -r sudo docker inspect >"$EVIDENCE_DIR/containers.json"
    python3 - "$EVIDENCE_DIR/containers.json" <<'PY'
import json
import pathlib
import sys

containers = json.loads(pathlib.Path(sys.argv[1]).read_text())
actual = {}
for container in containers:
    service = container["Config"]["Labels"]["com.docker.compose.service"]
    bindings = container["HostConfig"].get("PortBindings") or {}
    actual[service] = sorted(bindings)
expected = {
    "resolver": ["8090/tcp"],
    "kubo": ["8080/tcp"],
    "ar-io-envoy": ["3000/tcp"],
    "ar-io-core": [],
    "ar-io-redis": [],
    "ar-io-observer": [],
}
assert actual == expected, actual
PY
}

verify_state() {
    local cid payload lan_address
    cid=$(<"$EVIDENCE_DIR/cid")
    payload=$(<"$EVIDENCE_DIR/payload")
    lan_address=$(<"$EVIDENCE_DIR/lan-address")
    curl -fsS "http://$lan_address:3000/ar-io/info" >/dev/null \
        || fail "AR.IO does not answer on the configured LAN address"
    curl -fsS "http://$lan_address:8090/healthz" >/dev/null \
        || fail "resolver does not answer on the configured LAN address"
    [[ $(curl -fsS "http://$lan_address:8080/ipfs/$cid") == "$payload" ]] \
        || fail "Kubo did not retain the stored payload on its LAN gateway"
    compose exec -T kubo ipfs pin ls "$cid" >/dev/null \
        || fail "Kubo did not retain the stored pin"
    curl -fsS http://127.0.0.1:8090/favorites | grep -F "$cid" >/dev/null \
        || fail "favorite state was not retained"
    curl -fsS http://127.0.0.1:8090/override | grep -F 'sandbox.invalid/dead' >/dev/null \
        || fail "override state was not retained"
    sudo grep -F "$cid" /var/lib/curio/resolver/captures/captures.jsonl >/dev/null \
        || fail "capture provenance was not retained"
    [[ $(sudo sha256sum /etc/curio/curio.env | awk '{print $1}') == "$(<"$EVIDENCE_DIR/config.sha256")" ]] \
        || fail "installer changed curio.env"
    [[ $(sudo sha256sum /var/lib/curio/ar-io/start-height.env | awk '{print $1}') == "$(<"$EVIDENCE_DIR/start-height.sha256")" ]] \
        || fail "AR.IO START_HEIGHT changed"
    if [[ -f $EVIDENCE_DIR/arweave.txid ]]; then
        fetch_arweave_fixture "$(<"$EVIDENCE_DIR/arweave.txid")" \
            "$EVIDENCE_DIR/arweave.verify" "$EVIDENCE_DIR/arweave.verify.headers" \
            || fail "AR.IO could not retrieve the fixture after restart"
        [[ $(sha256sum "$EVIDENCE_DIR/arweave.verify" | awk '{print $1}') == "$(<"$EVIDENCE_DIR/arweave.sha256")" ]] \
            || fail "AR.IO returned different fixture bytes after restart"
        grep -Eiq '^x-cache:[[:space:]]*HIT' "$EVIDENCE_DIR/arweave.verify.headers" \
            || fail "AR.IO did not retain the fixture in its cache"
        rm -f -- "$EVIDENCE_DIR/arweave.verify" "$EVIDENCE_DIR/arweave.verify.headers"
    fi
    check_published_ports
}

sudo install -d -m 0755 -o "$(id -u)" -g "$(id -g)" "$EVIDENCE_DIR"

if [[ $MODE == --after-reboot ]]; then
    [[ -f $EVIDENCE_DIR/cid ]] || fail "no first-phase evidence found in $EVIDENCE_DIR"
    wait_healthy
    verify_state
    cp "$EVIDENCE_DIR/health.latest" "$EVIDENCE_DIR/health.after-reboot"
    echo "post-reboot checks passed; evidence: $EVIDENCE_DIR"
    exit 0
fi

lan_address=$(ip -4 route get 1.1.1.1 2>/dev/null \
    | awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}')
[[ -n $lan_address ]] || fail "could not determine the VM LAN IPv4 address"
printf '%s\n' "$lan_address" >"$EVIDENCE_DIR/lan-address"

sudo env CURIO_LAN_ADDRESS="$lan_address" "$ROOT/appliance/install.sh"
wait_healthy
check_published_ports

if [[ -n $ARWEAVE_TXID ]]; then
    fetch_arweave_fixture "$ARWEAVE_TXID" "$EVIDENCE_DIR/arweave.payload" \
        "$EVIDENCE_DIR/arweave.headers" \
        || fail "AR.IO could not retrieve the public fixture"
    actual_arweave_sha=$(sha256sum "$EVIDENCE_DIR/arweave.payload" | awk '{print $1}')
    [[ $actual_arweave_sha == "${ARWEAVE_SHA256,,}" ]] \
        || fail "AR.IO fixture checksum mismatch: got $actual_arweave_sha"
    printf '%s\n' "$ARWEAVE_TXID" >"$EVIDENCE_DIR/arweave.txid"
    printf '%s\n' "${ARWEAVE_SHA256,,}" >"$EVIDENCE_DIR/arweave.sha256"
fi

sudo sha256sum /etc/curio/curio.env | awk '{print $1}' >"$EVIDENCE_DIR/config.sha256"
sudo sha256sum /var/lib/curio/ar-io/start-height.env | awk '{print $1}' >"$EVIDENCE_DIR/start-height.sha256"
printf 'curio-sandbox-persistence-%s\n' "$(date +%s)" >"$EVIDENCE_DIR/payload"

store_response=$(curl -fsS -F "file=@$EVIDENCE_DIR/payload" http://127.0.0.1:8090/store)
printf '%s\n' "$store_response" >"$EVIDENCE_DIR/store.json"
printf '%s' "$store_response" | python3 -c 'import json,sys; print(json.load(sys.stdin)["cid"])' \
    >"$EVIDENCE_DIR/cid"
cid=$(<"$EVIDENCE_DIR/cid")

curl -fsS -X POST --get --data-urlencode "ref=ipfs://$cid" \
    http://127.0.0.1:8090/favorites >"$EVIDENCE_DIR/favorite.json"
curl -fsS -X POST -H 'content-type: application/json' \
    -d "{\"ref\":\"https://sandbox.invalid/dead\",\"replacement\":\"ipfs://$cid\",\"status\":\"operator-attested\",\"note\":\"disposable appliance test\"}" \
    http://127.0.0.1:8090/override >"$EVIDENCE_DIR/override.json"

# Rerun convergence: neither operator configuration nor first-deploy height may change.
sudo "$ROOT/appliance/install.sh"
wait_healthy
verify_state

# Container replacement must leave every bind-mounted state class intact.
compose up --detach --force-recreate --no-build
wait_healthy
verify_state

# Health must name a deliberately failed component and recover cleanly.
compose stop kubo
if sudo curio health >"$EVIDENCE_DIR/health.kubo-stopped" 2>&1; then
    fail "curio health succeeded while Kubo was stopped"
fi
grep -F 'FAIL kubo' "$EVIDENCE_DIR/health.kubo-stopped" >/dev/null \
    || fail "health output did not identify Kubo"
sudo curio start
wait_healthy
verify_state

cp "$EVIDENCE_DIR/health.latest" "$EVIDENCE_DIR/health.before-reboot"
echo "first-phase checks passed; evidence: $EVIDENCE_DIR"
echo "Reboot this disposable VM, then run: dev/test-appliance.sh --after-reboot"

#!/usr/bin/env bash
# Qualification of the shipped appliance without privileged host mutation.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf -- "$TMP"' EXIT
fail() { echo "test-appliance: $*" >&2; exit 1; }
for command in python3 tar sha256sum; do command -v "$command" >/dev/null || fail "$command is required"; done

# Bootstrap checksum and requested-version verification must happen before the
# embedded installer runs. A fake installer records that it was reached.
mkdir -p "$TMP/release" "$TMP/tree/curio/appliance" "$TMP/tree/curio/resolver" "$TMP/bin"
printf '[project]\nversion = "0.2.0"\n' >"$TMP/tree/curio/resolver/pyproject.toml"
cat >"$TMP/tree/curio/appliance/install.sh" <<EOF
#!/bin/sh
printf reached >"$TMP/reached"
EOF
chmod +x "$TMP/tree/curio/appliance/install.sh"
cat >"$TMP/bin/uname" <<'EOF'
#!/bin/sh
echo Linux
EOF
chmod +x "$TMP/bin/uname"
tar -czf "$TMP/release/curio-appliance.tar.gz" -C "$TMP/tree" curio
(cd "$TMP/release" && sha256sum curio-appliance.tar.gz >curio-appliance.tar.gz.sha256)
PATH="$TMP/bin:$PATH" CURIO_RELEASE_BASE_URL="file://$TMP/release" CURIO_VERSION=v0.2.0 "$ROOT/install.sh" >/dev/null
[[ $(<"$TMP/reached") == reached ]] || fail "verified bootstrap did not invoke release installer"
printf '%064d  curio-appliance.tar.gz\n' 0 >"$TMP/release/curio-appliance.tar.gz.sha256"
if PATH="$TMP/bin:$PATH" CURIO_RELEASE_BASE_URL="file://$TMP/release" "$ROOT/install.sh" >/dev/null 2>&1; then fail "bootstrap accepted bad checksum"; fi

# Render the real graph when Compose is available. The graph deliberately
# retains all r81 services/configuration while exposing only Curio's front door
# (plus IPFS swarm participation), and resolver waits on real Envoy health.
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  DATA="$TMP/data"; APP="$TMP/app"; mkdir -p "$DATA/ar-io" "$APP/current"
  printf 'START_HEIGHT=0\n' >"$DATA/ar-io/start-height.env"
  cat >"$TMP/curio.env" <<EOF
CURIO_APP_ROOT=$APP
CURIO_DATA_ROOT=$DATA
CURIO_HOST_UID=$(id -u)
CURIO_HOST_GID=$(id -g)
CURIO_CURATOR_TOKEN=test
EOF
  docker compose --project-name curio-qualification --env-file "$TMP/curio.env" --file "$ROOT/appliance/compose.yaml" config --format json >"$TMP/compose.json"
  python3 - "$TMP/compose.json" "$DATA" <<'PY'
import json, pathlib, sys
c=json.loads(pathlib.Path(sys.argv[1]).read_text()); s=c['services']; data=pathlib.Path(sys.argv[2])
assert set(s)=={'resolver','kubo','ar-io-redis','ar-io-core','ar-io-observer','ar-io-envoy'}
assert s['resolver']['depends_on']['ar-io-envoy']['condition']=='service_healthy'
assert s['ar-io-envoy']['healthcheck']['test'] == ['CMD','curl','-fsS','http://127.0.0.1:3000/ar-io/info']
assert s['ar-io-observer']['healthcheck']['disable'] is True
core=s['ar-io-core']['environment']
for key in ('RUN_OBSERVER','TRUSTED_GATEWAYS_URLS','ON_DEMAND_RETRIEVAL_ORDER','ANS104_UNBUNDLE_FILTER','ANS104_INDEX_FILTER','CONTIGUOUS_DATA_CACHE_CLEANUP_THRESHOLD'):
    assert key in core, key
assert core['RUN_OBSERVER']=='false' and core['ON_DEMAND_RETRIEVAL_ORDER'].split(',')[0]=='trusted-gateways'
envoy=s['ar-io-envoy']['environment']
for key in ('TVAL_OBSERVER_HOST','TVAL_FALLBACK_NODE_HOST','TVAL_GRAPHQL_HOST','TVAL_DATASETS_HOST','TVAL_ENABLE_ARWEAVE_PEER_EDS','TVAL_ARIO_GATEWAY_UPSTREAM_IDLE_TIMEOUT'):
    assert key in envoy, key
ports=[(n,p['target']) for n,x in s.items() for p in x.get('ports',[])]
assert ('resolver',8090) in ports and ('kubo',4001) in ports
assert not s['kubo'].get('ports',[])[0]['target']==8080
assert not s['ar-io-envoy'].get('ports',[])
assert s['resolver']['environment']['RESOLVER_ARWEAVE_INTERNAL']=='http://ar-io-envoy:3000'
assert 'RESOLVER_IPFS_PUBLIC_BASE' not in s['resolver']['environment']
PY
else
  echo 'docker compose unavailable: compose rendering skipped' >&2
fi

# Kubo init converges and does not contain a destructive GC/pin removal.
mkdir -p "$TMP/kubo-bin" "$TMP/kubo-state"
cat >"$TMP/kubo-bin/ipfs" <<'EOF'
#!/bin/sh
[ "$1" = config ] && [ "$2" = --json ] || exit 99
f="$KUBO_TEST_STATE/$3"; if [ "$#" = 3 ]; then [ -f "$f" ] || exit 1; cat "$f"; else printf %s "$4" >"$f"; printf '%s=%s\n' "$3" "$4" >>"$KUBO_TEST_STATE/writes"; fi
EOF
chmod +x "$TMP/kubo-bin/ipfs"; : >"$TMP/kubo-state/writes"
PATH="$TMP/kubo-bin:$PATH" KUBO_TEST_STATE="$TMP/kubo-state" CURIO_IPFS_STORAGE_MAX=42GB "$ROOT/appliance/kubo-init.sh"
PATH="$TMP/kubo-bin:$PATH" KUBO_TEST_STATE="$TMP/kubo-state" CURIO_IPFS_STORAGE_MAX=42GB "$ROOT/appliance/kubo-init.sh"
[[ $(awk 'END{print NR}' "$TMP/kubo-state/writes") == 6 ]] || fail 'Kubo init was not convergent'
! grep -E 'repo[[:space:]]+gc|pin[[:space:]]+rm' "$ROOT/appliance/kubo-init.sh"

# A fake Docker run exercises per-user custom-root installation and an update:
# current is atomically a symlink to a release, state/config survive, and no
# caller root is recursively replaced.
cat >"$TMP/bin/docker" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >>"$CURIO_DOCKER_LOG"
case "$*" in *'compose version'*|*' info'*|*' config --quiet'*|*' build resolver'*|*' up -d'*) exit 0;; esac
exit 0
EOF
chmod +x "$TMP/bin/docker"; : >"$TMP/docker.log"
XDG_DATA_HOME="$TMP/xdg-data" XDG_CONFIG_HOME="$TMP/xdg-config" XDG_BIN_HOME="$TMP/bin-out" CURIO_APP_ROOT="$TMP/custom-app" CURIO_DATA_ROOT="$TMP/custom-state" CURIO_DOCKER_LOG="$TMP/docker.log" PATH="$TMP/bin:$PATH" "$ROOT/appliance/install.sh" >/dev/null
[[ -L $TMP/custom-app/current ]] || fail 'installer did not create current release pointer'
[[ -d $TMP/custom-state/ipfs && -f $TMP/xdg-config/curio/curio.env ]] || fail 'installer did not persist user state'
printf preserved >"$TMP/custom-state/sentinel"
XDG_DATA_HOME="$TMP/xdg-data" XDG_CONFIG_HOME="$TMP/xdg-config" XDG_BIN_HOME="$TMP/bin-out" CURIO_APP_ROOT="$TMP/custom-app" CURIO_DOCKER_LOG="$TMP/docker.log" PATH="$TMP/bin:$PATH" "$ROOT/appliance/install.sh" >/dev/null
[[ $(<"$TMP/custom-state/sentinel") == preserved ]] || fail 'rerun damaged persistent state'
CURIO_DOCKER_BIN="$TMP/bin/docker" CURIO_DOCKER_LOG="$TMP/docker.log" CURIO_ENV_FILE="$TMP/xdg-config/curio/curio.env" "$TMP/bin-out/curio" status

grep -q -- '--file .*custom-app/current/compose.yaml' "$TMP/docker.log" || fail 'wrapper did not discover configured custom app root'
sh -n "$ROOT/install.sh" "$ROOT/appliance/install.sh" "$ROOT/appliance/curio" "$ROOT/appliance/kubo-init.sh"
echo 'appliance tests passed'

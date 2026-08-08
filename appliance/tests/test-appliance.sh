#!/usr/bin/env bash
# Qualification of the shipped appliance without privileged host mutation.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
TMP=$(mktemp -d)
trap 'if [[ -f $TMP/pyproject.orig ]]; then cp "$TMP/pyproject.orig" "$ROOT/resolver/pyproject.toml"; fi; rm -rf -- "$TMP"' EXIT
fail() { echo "test-appliance: $*" >&2; exit 1; }
for command in python3 tar sha256sum; do command -v "$command" >/dev/null || fail "$command is required"; done
grep -q 'RESOLVER_TRUSTED_PROXY_CIDRS: ${CURIO_TRUSTED_PROXY_CIDRS:-}' "$ROOT/appliance/compose.yaml" || fail 'Compose does not map trusted proxy CIDRs'

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

# Render the three-service graph when Compose is available. AR.IO has one
# persistent Core; resolver and Kubo are its only peers in this graph.
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
assert set(s)=={'resolver','kubo','ar-io-core'}
assert s['resolver']['depends_on']['ar-io-core']['condition']=='service_healthy'
assert s['resolver']['healthcheck']['test'][0] == 'CMD'
core=s['ar-io-core']['environment']
for key in ('RUN_OBSERVER','TRUSTED_GATEWAYS_URLS','ON_DEMAND_RETRIEVAL_ORDER','ANS104_UNBUNDLE_FILTER','ANS104_INDEX_FILTER','CHAIN_CACHE_TYPE','ENABLE_CHUNK_DATA_CACHE_CLEANUP'):
    assert key in core, key
assert core['RUN_OBSERVER']=='false'
assert core['TRUSTED_NODE_URL']=='https://arweave.net'
assert core['CHAIN_CACHE_TYPE']=='lmdb'
assert core['ENABLE_CHUNK_DATA_CACHE_CLEANUP']=='false'
assert 'REDIS_CACHE_URL' not in core and 'CONTIGUOUS_DATA_CACHE_CLEANUP_THRESHOLD' not in core
assert core['ON_DEMAND_RETRIEVAL_ORDER'].split(',')[0]=='trusted-gateways'
assert 'https://arweave.net' in core['TRUSTED_GATEWAYS_URLS']
assert s['ar-io-core']['user'] == f'{__import__("os").getuid()}:{__import__("os").getgid()}'
assert not s['ar-io-core'].get('ports', [])
ports=[(n,p['target']) for n,x in s.items() for p in x.get('ports',[])]
assert ('resolver',8090) in ports and ('kubo',4001) in ports
assert not s['kubo'].get('ports',[])[0]['target']==8080
assert s['resolver']['environment']['RESOLVER_ARWEAVE_INTERNAL']=='http://ar-io-core:4000'
assert s['resolver']['environment']['RESOLVER_ARWEAVE_COLD_TIMEOUT']=='300'
assert s['resolver']['environment']['RESOLVER_TRUSTED_PROXY_CIDRS']==''
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
case "$*" in
  *'compose version'*|*' info'*|*' config --quiet'*|*' build resolver'*) exit 0;;
  *' pull ghcr.io/ar-io/ar-io-core:'*) exit 0;;
  *run\ --rm\ *ar-io-core:*) printf '%s\n' "${CURIO_TEST_HEIGHT:-123456}"; exit 0;;
  *up\ -d*)
    if [ "${CURIO_DOCKER_FAIL_UP:-0}" = 1 ] && [ ! -e "${CURIO_DOCKER_FAIL_ONCE_FILE:?}" ]; then touch "$CURIO_DOCKER_FAIL_ONCE_FILE"; exit 42; fi
    exit 0;;
esac
exit 0
EOF
chmod +x "$TMP/bin/docker"
# The qualification host may be BSD while install.sh deliberately requires
# GNU mv -T on Linux. Simulate that Linux primitive for the fake-Docker run.
cat >"$TMP/bin/mv" <<'EOF'
#!/bin/sh
if [ "$1" = -Tf ]; then
  shift
  rm -f "$2"
fi
exec /bin/mv -f "$@"
EOF
chmod +x "$TMP/bin/mv"; : >"$TMP/docker.log"
XDG_DATA_HOME="$TMP/xdg-data" XDG_CONFIG_HOME="$TMP/xdg-config" XDG_BIN_HOME="$TMP/bin-out" CURIO_APP_ROOT="$TMP/custom-app" CURIO_DATA_ROOT="$TMP/custom-state" CURIO_DOCKER_LOG="$TMP/docker.log" PATH="$TMP/bin:$PATH" "$ROOT/appliance/install.sh" >/dev/null
[[ -L $TMP/custom-app/current ]] || fail 'installer did not create current release pointer'
[[ -d $TMP/custom-state/ipfs && -f $TMP/xdg-config/curio/curio.env ]] || fail 'installer did not persist user state'
grep -q '^CURIO_TRUSTED_PROXY_CIDRS=$' "$TMP/xdg-config/curio/curio.env" || fail 'installer did not persist trusted proxy setting'
[[ $(<"$TMP/custom-state/ar-io/start-height.env") == START_HEIGHT=123456 ]] || fail 'first install did not discover AR.IO height'
first_pointer=$(readlink "$TMP/custom-app/current"); first_version=$(<"$TMP/custom-app/current/VERSION")
# Simulate the next verified release: an update must replace both pointer and
# installed package version, not nest a link below the old release directory.
cp "$ROOT/resolver/pyproject.toml" "$TMP/pyproject.orig"
python3 - "$ROOT/resolver/pyproject.toml" <<'PY'
from pathlib import Path
path = Path(__import__('sys').argv[1])
path.write_text(path.read_text().replace('version = "0.2.0"', 'version = "0.2.1"', 1))
PY
printf preserved >"$TMP/custom-state/sentinel"
# Upgrade must not recursively remove historical state or rewrite obsolete keys.
mkdir -p "$TMP/custom-state/ar-io/redis" "$TMP/custom-state/ar-io/envoy-eds" "$TMP/custom-state/ar-io-retained/redis"
printf legacy >"$TMP/custom-state/ar-io-retained/redis/sentinel"
printf 'CURIO_REDIS_MAX_MEMORY=256mb\n' >>"$TMP/xdg-config/curio/curio.env"
XDG_DATA_HOME="$TMP/xdg-data" XDG_CONFIG_HOME="$TMP/xdg-config" XDG_BIN_HOME="$TMP/bin-out" CURIO_APP_ROOT="$TMP/custom-app" CURIO_DOCKER_LOG="$TMP/docker.log" PATH="$TMP/bin:$PATH" "$ROOT/appliance/install.sh" >/dev/null
second_pointer=$(readlink "$TMP/custom-app/current")
[[ $second_pointer != "$first_pointer" && $(<"$TMP/custom-app/current/VERSION") != "$first_version" ]] || fail 'second install did not replace current release pointer and version'
[[ $(<"$TMP/custom-state/ar-io/start-height.env") == START_HEIGHT=123456 ]] || fail 'rerun changed AR.IO start height'
[[ $(<"$TMP/custom-state/sentinel") == preserved ]] || fail 'rerun damaged persistent state'
[[ $(<"$TMP/custom-state/ar-io-retained/redis/sentinel") == legacy ]] || fail 'rerun removed obsolete state'
grep -q '^CURIO_REDIS_MAX_MEMORY=256mb$' "$TMP/xdg-config/curio/curio.env" || fail 'rerun rewrote obsolete configuration'
if XDG_DATA_HOME="$TMP/xdg-data" XDG_CONFIG_HOME="$TMP/xdg-config" XDG_BIN_HOME="$TMP/bin-out" CURIO_APP_ROOT="$TMP/custom-app" CURIO_DATA_ROOT="$TMP/conflicting-state" CURIO_DOCKER_LOG="$TMP/docker.log" PATH="$TMP/bin:$PATH" "$ROOT/appliance/install.sh" >/dev/null 2>&1; then fail 'installer accepted conflicting CURIO_DATA_ROOT on rerun'; fi
# Failed start/health must restore the previous pointer and deployment.
up_before=$(grep -c 'up -d' "$TMP/docker.log" || true)
if XDG_DATA_HOME="$TMP/xdg-data" XDG_CONFIG_HOME="$TMP/xdg-config" XDG_BIN_HOME="$TMP/bin-out" CURIO_APP_ROOT="$TMP/custom-app" CURIO_DOCKER_LOG="$TMP/docker.log" CURIO_DOCKER_FAIL_UP=1 CURIO_DOCKER_FAIL_ONCE_FILE="$TMP/fail-once" PATH="$TMP/bin:$PATH" "$ROOT/appliance/install.sh" >/dev/null 2>&1; then fail 'installer accepted failed Compose health'; fi
[[ $(readlink "$TMP/custom-app/current") == "$second_pointer" ]] || fail 'failed install did not roll back current pointer'
[[ $(grep -c 'up -d' "$TMP/docker.log") -eq $((up_before + 2)) ]] || fail 'failed install did not restore prior Compose deployment'
grep -q 'down --remove-orphans' "$TMP/docker.log" || fail 'failed install did not remove partial Compose project'
grep -q 'up -d --wait --wait-timeout .* --remove-orphans' "$TMP/docker.log" || fail 'upgrade did not remove obsolete Compose services'
# The installed wrapper must call the root verified bootstrap (not the
# appliance installer directly), preserving an exact tag and empty/latest mode.
grep -q 'Verified Curio release bootstrap' "$TMP/custom-app/current/install.sh" || fail 'installed update target is not the verified bootstrap'
cat >"$TMP/custom-app/current/install.sh" <<EOF
#!/bin/sh
printf 'version=%s app=%s\\n' "\${CURIO_VERSION-unset}" "\${CURIO_APP_ROOT-unset}" >>"$TMP/wrapper-update.log"
EOF
chmod +x "$TMP/custom-app/current/install.sh"
CURIO_DOCKER_BIN="$TMP/bin/docker" CURIO_ENV_FILE="$TMP/xdg-config/curio/curio.env" "$TMP/bin-out/curio" update --version v0.2.1
CURIO_DOCKER_BIN="$TMP/bin/docker" CURIO_ENV_FILE="$TMP/xdg-config/curio/curio.env" "$TMP/bin-out/curio" update
[[ $(sed -n '1p' "$TMP/wrapper-update.log") == "version=v0.2.1 app=$TMP/custom-app" ]] || fail 'wrapper did not propagate exact update version to bootstrap'
[[ $(sed -n '2p' "$TMP/wrapper-update.log") == "version= app=$TMP/custom-app" ]] || fail 'wrapper did not invoke latest bootstrap mode'
CURIO_DOCKER_BIN="$TMP/bin/docker" CURIO_DOCKER_LOG="$TMP/docker.log" CURIO_ENV_FILE="$TMP/xdg-config/curio/curio.env" "$TMP/bin-out/curio" status

grep -q -- '--file .*custom-app/current/compose.yaml' "$TMP/docker.log" || fail 'wrapper did not discover configured custom app root'
sh -n "$ROOT/install.sh" "$ROOT/appliance/install.sh" "$ROOT/appliance/curio" "$ROOT/appliance/kubo-init.sh"
echo 'appliance tests passed'

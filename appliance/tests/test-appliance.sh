#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf -- "$TMP"' EXIT

fail() {
    echo "test-appliance: $*" >&2
    exit 1
}

command -v docker >/dev/null 2>&1 || fail "docker CLI is required for Compose rendering"
docker compose version >/dev/null 2>&1 || fail "docker compose plugin is required"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
for command in curl tar sha256sum; do
    command -v "$command" >/dev/null 2>&1 || fail "$command is required"
done

# The public curl bootstrap must verify an archive before invoking the inner
# installer, and it must carry unattended settings across its sudo boundary.
mkdir -p "$TMP/release" "$TMP/release-tree/curio/appliance" \
    "$TMP/release-tree/curio/resolver" "$TMP/bootstrap-bin"
cat >"$TMP/release-tree/curio/resolver/pyproject.toml" <<'EOF'
[project]
version = "0.1.0"
EOF
cat >"$TMP/release-tree/curio/appliance/install.sh" <<EOF
#!/bin/sh
printf '%s\n' "\$CURIO_LAN_ADDRESS" >"$TMP/bootstrap-ran"
EOF
chmod 0755 "$TMP/release-tree/curio/appliance/install.sh"
tar -czf "$TMP/release/curio-appliance.tar.gz" -C "$TMP/release-tree" curio
(
    cd "$TMP/release"
    sha256sum curio-appliance.tar.gz >curio-appliance.tar.gz.sha256
)
cat >"$TMP/bootstrap-bin/sudo" <<'EOF'
#!/bin/sh
exec "$@"
EOF
cat >"$TMP/bootstrap-bin/uname" <<'EOF'
#!/bin/sh
printf 'Linux\n'
EOF
chmod 0755 "$TMP/bootstrap-bin/sudo" "$TMP/bootstrap-bin/uname"
PATH="$TMP/bootstrap-bin:$PATH" \
CURIO_RELEASE_BASE_URL="file://$TMP/release" \
CURIO_LAN_ADDRESS=192.0.2.30 \
    "$ROOT/install.sh" >/dev/null
[[ $(<"$TMP/bootstrap-ran") == 192.0.2.30 ]] \
    || fail "release bootstrap did not invoke the installer with its LAN setting"
printf '%064d  curio-appliance.tar.gz\n' 0 \
    >"$TMP/release/curio-appliance.tar.gz.sha256"
if PATH="$TMP/bootstrap-bin:$PATH" \
    CURIO_RELEASE_BASE_URL="file://$TMP/release" "$ROOT/install.sh" >/dev/null 2>&1; then
    fail "release bootstrap accepted an invalid archive checksum"
fi

DATA_ROOT="$TMP/data"
mkdir -p "$DATA_ROOT/ar-io"
printf 'START_HEIGHT=123456\n' >"$DATA_ROOT/ar-io/start-height.env"
cat >"$TMP/curio.env" <<EOF
CURIO_LAN_ADDRESS=192.0.2.10
CURIO_DATA_ROOT=$DATA_ROOT
CURIO_RESOLVER_BUILD_CONTEXT=$ROOT/resolver
CURIO_APPLIANCE_ROOT=$ROOT/appliance
EOF

docker compose \
    --project-name curio-config-test \
    --env-file "$TMP/curio.env" \
    --file "$ROOT/appliance/compose.yaml" \
    config --format json >"$TMP/compose.json"

python3 - "$TMP/compose.json" "$DATA_ROOT" "$ROOT/appliance/kubo-init.sh" <<'PY'
import json
import pathlib
import sys

config = json.loads(pathlib.Path(sys.argv[1]).read_text())
data_root = pathlib.Path(sys.argv[2])
kubo_init = pathlib.Path(sys.argv[3])
services = config["services"]
expected_services = {
    "resolver",
    "kubo",
    "ar-io-core",
    "ar-io-envoy",
    "ar-io-redis",
    "ar-io-observer",
}
assert set(services) == expected_services, set(services)

published = []
for name, service in services.items():
    for port in service.get("ports", []):
        published.append((name, int(port["published"]), int(port["target"]), port["protocol"]))
assert sorted(published) == [
    ("ar-io-envoy", 3000, 3000, "tcp"),
    ("kubo", 8080, 8080, "tcp"),
    ("resolver", 8090, 8090, "tcp"),
], published
for private in ("ar-io-core", "ar-io-redis", "ar-io-observer"):
    assert not services[private].get("ports"), (private, services[private].get("ports"))
assert all(port[2] != 5001 for port in published), published

persistent_targets = {
    ("resolver", "/state"),
    ("kubo", "/data/ipfs"),
    ("ar-io-core", "/app/data"),
    ("ar-io-envoy", "/data/envoy-eds"),
    ("ar-io-redis", "/data"),
}
seen_targets = set()
for name, service in services.items():
    for volume in service.get("volumes", []):
        assert volume["type"] == "bind", volume
        source = pathlib.Path(volume["source"])
        target = volume["target"]
        if (name, target) in persistent_targets:
            assert source == data_root or data_root in source.parents, volume
            seen_targets.add((name, target))
        else:
            assert name == "kubo" and target == "/container-init.d/10-curio-config.sh", volume
            assert source == kubo_init, volume
            assert volume.get("read_only") is True, volume
        assert "docker.sock" not in str(source), volume
assert seen_targets == persistent_targets, seen_targets

for name, service in services.items():
    image = service.get("image", "")
    assert image and ":latest" not in image, (name, image)
    assert service.get("restart") == "unless-stopped", (name, service.get("restart"))
    assert not service.get("privileged", False), name

assert services["kubo"]["user"] == "1000:100"
assert services["ar-io-redis"]["user"] == "999:1000"
kubo_env = services["kubo"]["environment"]
assert kubo_env["CURIO_IPFS_STORAGE_MAX"] == "20GB"
resolver_env = services["resolver"]["environment"]
assert resolver_env["RESOLVER_IPFS_API"] == "http://kubo:5001"
assert resolver_env["RESOLVER_ARWEAVE_INTERNAL"] == "http://ar-io-envoy:3000"
assert resolver_env["RESOLVER_IPFS_PUBLIC_BASE"] == "http://192.0.2.10:8080"
assert resolver_env["RESOLVER_ARWEAVE_PUBLIC_BASE"] == "http://192.0.2.10:3000"
core_env = services["ar-io-core"]["environment"]
assert core_env["START_HEIGHT"] == "123456"
assert core_env["NODE_OPTIONS"] == "--dns-result-order=ipv4first"
assert core_env["RUN_OBSERVER"] == "false"
assert core_env["ANS104_UNBUNDLE_FILTER"] == '{"never":true}'
assert core_env["ANS104_INDEX_FILTER"] == '{"never":true}'
assert core_env["ON_DEMAND_RETRIEVAL_ORDER"].split(",")[0] == "trusted-gateways"
trusted_gateways = json.loads(core_env["TRUSTED_GATEWAYS_URLS"])
assert trusted_gateways == {
    "https://ar-io.dev": 1,
    "https://turbo-gateway.com": 2,
    "https://permagate.io": {"priority": 3, "trusted": False},
    "https://arweave.net": {"priority": 4, "trusted": False},
}
assert services["ar-io-observer"]["healthcheck"]["disable"] is True
envoy_env = services["ar-io-envoy"]["environment"]
required_envoy_template_values = {
    "TVAL_ARIO_GATEWAY_UPSTREAM_IDLE_TIMEOUT",
    "TVAL_ARNS_ROOT_HOST",
    "TVAL_ARWEAVE_POST_DRY_RUN",
    "TVAL_AR_IO_HOST",
    "TVAL_AR_IO_PORT",
    "TVAL_DATASETS_HOST",
    "TVAL_DATASETS_PORT",
    "TVAL_ENABLE_ARWEAVE_PEER_EDS",
    "TVAL_FALLBACK_NODE_HOST",
    "TVAL_FALLBACK_NODE_PORT",
    "TVAL_GRAPHQL_HOST",
    "TVAL_GRAPHQL_HOST_HEADER",
    "TVAL_GRAPHQL_PORT",
    "TVAL_OBSERVER_HOST",
    "TVAL_OBSERVER_PORT",
    "TVAL_TRUSTED_NODE_HOST",
    "TVAL_TRUSTED_NODE_PORT",
}
assert required_envoy_template_values <= set(envoy_env), sorted(
    required_envoy_template_values - set(envoy_env)
)
PY

# The same function used by main must create an initial environment atomically
# and leave an existing file byte-for-byte unchanged.
mkdir -p "$TMP/etc"
printf 'DO_NOT_REPLACE=yes\n' >"$TMP/etc/existing.env"
cp "$TMP/etc/existing.env" "$TMP/etc/existing.before"
CURIO_INSTALL_SH_SOURCE_ONLY=1 sh -c '
    . "$1"
    write_curio_env_if_missing "$2" 192.0.2.20 /var/lib/other 99GB
' _ "$ROOT/appliance/install.sh" "$TMP/etc/existing.env"
cmp "$TMP/etc/existing.before" "$TMP/etc/existing.env" \
    || fail "write_curio_env_if_missing overwrote an existing environment"

CURIO_INSTALL_SH_SOURCE_ONLY=1 sh -c '
    . "$1"
    write_curio_env_if_missing "$2" 192.0.2.20 /var/lib/curio 20GB
' _ "$ROOT/appliance/install.sh" "$TMP/etc/new.env"
grep -Fx 'CURIO_LAN_ADDRESS=192.0.2.20' "$TMP/etc/new.env" >/dev/null
grep -Fx 'CURIO_DATA_ROOT=/var/lib/curio' "$TMP/etc/new.env" >/dev/null

# Application updates must not retain Python modules that were deleted from
# the release source. This cleanup is limited to the installed build context;
# persistent state lives elsewhere and is never passed to this function.
mkdir -p "$TMP/resolver-source/src/resolver" "$TMP/resolver-installed/src/resolver/old"
printf 'new\n' >"$TMP/resolver-source/src/resolver/keep.py"
printf 'old\n' >"$TMP/resolver-installed/src/resolver/keep.py"
printf 'stale\n' >"$TMP/resolver-installed/src/resolver/old/removed.py"
CURIO_INSTALL_SH_SOURCE_ONLY=1 sh -c '
    . "$1"
    prune_resolver_context "$2" "$3"
' _ "$ROOT/appliance/install.sh" "$TMP/resolver-source" "$TMP/resolver-installed"
[[ -f $TMP/resolver-installed/src/resolver/keep.py ]] \
    || fail "resolver context pruning removed a current source file"
[[ ! -e $TMP/resolver-installed/src/resolver/old/removed.py ]] \
    || fail "resolver context pruning retained a deleted source file"
[[ ! -d $TMP/resolver-installed/src/resolver/old ]] \
    || fail "resolver context pruning retained an empty stale directory"

# Kubo's pre-daemon hook must converge: a second run against the settings from
# the first run may not write configuration again.
mkdir -p "$TMP/kubo-bin" "$TMP/kubo-state"
cat >"$TMP/kubo-bin/ipfs" <<'EOF'
#!/bin/sh
set -eu
[ "$1" = config ] || exit 99
[ "$2" = --json ] || exit 98
key=$3
state_file="$KUBO_TEST_STATE/${key}.value"
if [ "$#" -eq 3 ]; then
    [ -f "$state_file" ] || exit 1
    cat "$state_file"
else
    printf '%s' "$4" >"$state_file"
    printf '%s=%s\n' "$key" "$4" >>"$KUBO_TEST_STATE/writes"
fi
EOF
chmod 0755 "$TMP/kubo-bin/ipfs"
: >"$TMP/kubo-state/writes"
PATH="$TMP/kubo-bin:$PATH" KUBO_TEST_STATE="$TMP/kubo-state" \
    CURIO_IPFS_STORAGE_MAX=42GB "$ROOT/appliance/kubo-init.sh" >/dev/null
PATH="$TMP/kubo-bin:$PATH" KUBO_TEST_STATE="$TMP/kubo-state" \
    CURIO_IPFS_STORAGE_MAX=42GB "$ROOT/appliance/kubo-init.sh" >/dev/null
[[ $(wc -l <"$TMP/kubo-state/writes") -eq 6 ]] \
    || fail "Kubo initialization rewrote settings on its second run"
grep -Fx 'Datastore.StorageMax="42GB"' "$TMP/kubo-state/writes" >/dev/null
grep -Fx 'Routing.Type="autoclient"' "$TMP/kubo-state/writes" >/dev/null
if grep -E 'repo[[:space:]]+gc|pin[[:space:]]+rm' "$ROOT/appliance/kubo-init.sh" >/dev/null; then
    fail "Kubo initialization contains a destructive operation"
fi

# Exercise operator command construction with a fake Docker CLI. The wrapper
# must provide its own project, environment, and Compose paths from any cwd.
mkdir -p "$TMP/bin"
cat >"$TMP/bin/docker" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >>"$CURIO_TEST_DOCKER_LOG"
exit 0
EOF
chmod 0755 "$TMP/bin/docker"
: >"$TMP/operator-compose.yaml"
: >"$TMP/operator.env"
: >"$TMP/docker.log"

run_curio() {
    (
        cd /
        CURIO_DOCKER_BIN="$TMP/bin/docker" \
        CURIO_TEST_DOCKER_LOG="$TMP/docker.log" \
        CURIO_COMPOSE_FILE="$TMP/operator-compose.yaml" \
        CURIO_ENV_FILE="$TMP/operator.env" \
        "$ROOT/appliance/curio" "$@"
    )
}

run_curio start
run_curio stop
run_curio status
run_curio restart kubo
run_curio logs resolver --follow --tail 10
if run_curio health >"$TMP/health.out" 2>&1; then
    fail "curio health unexpectedly accepted missing containers"
fi

grep -Fx "compose --project-name curio --env-file $TMP/operator.env --file $TMP/operator-compose.yaml up --detach" "$TMP/docker.log" >/dev/null \
    || fail "curio start constructed the wrong Compose invocation"
grep -Fx "compose --project-name curio --env-file $TMP/operator.env --file $TMP/operator-compose.yaml stop" "$TMP/docker.log" >/dev/null \
    || fail "curio stop constructed the wrong Compose invocation"
grep -Fx "compose --project-name curio --env-file $TMP/operator.env --file $TMP/operator-compose.yaml ps" "$TMP/docker.log" >/dev/null \
    || fail "curio status constructed the wrong Compose invocation"
grep -Fx "compose --project-name curio --env-file $TMP/operator.env --file $TMP/operator-compose.yaml restart kubo" "$TMP/docker.log" >/dev/null \
    || fail "curio restart constructed the wrong Compose invocation"
grep -Fx "compose --project-name curio --env-file $TMP/operator.env --file $TMP/operator-compose.yaml logs --tail 200 --follow --tail 10 resolver" "$TMP/docker.log" >/dev/null \
    || fail "curio logs constructed the wrong Compose invocation"
grep -Fx "compose --project-name curio --env-file $TMP/operator.env --file $TMP/operator-compose.yaml ps --all --quiet kubo" "$TMP/docker.log" >/dev/null \
    || fail "curio health did not inspect Kubo through installed Compose"
grep -Fx "compose --project-name curio --env-file $TMP/operator.env --file $TMP/operator-compose.yaml exec -T resolver python -" "$TMP/docker.log" >/dev/null \
    || fail "curio health did not query the resolver aggregate"
if run_curio restart not-a-service >/dev/null 2>&1; then
    fail "curio accepted an unknown service"
fi

echo "appliance tests passed"

#!/bin/sh
set -eu

AR_IO_ENVOY_IMAGE='ghcr.io/ar-io/ar-io-envoy:bd738a2435f1293e259dfcbb4ef42f50b26545da@sha256:ffbf849370ed24b2dfecab2ceef733bf1fb67da2d14b4909063d788cc8f72282'
RESOLVER_UID=10001
RESOLVER_GID=10001
KUBO_UID=1000
KUBO_GID=100
REDIS_UID=999
REDIS_GID=1000
CURRENT_STEP='initial checks'
INSTALL_SUCCEEDED=0

fail() {
    echo "install.sh: $*" >&2
    exit 1
}

on_exit() {
    status=$?
    if [ "$status" -ne 0 ] && [ "$INSTALL_SUCCEEDED" -ne 1 ]; then
        echo "install.sh: failed during: $CURRENT_STEP" >&2
        echo "No Curio configuration or state was removed. Fix the error and rerun the installer." >&2
    fi
    return "$status"
}

valid_ipv4() {
    printf '%s\n' "$1" | awk -F. '
        {
            if (NF != 4 || $1 == 127 || $0 == "0.0.0.0") bad = 1
            for (i = 1; i <= NF; i++) {
                if ($i !~ /^[0-9]+$/ || $i + 0 > 255) bad = 1
            }
        }
        END { exit(bad || NR != 1) }
    '
}

valid_data_root() {
    value=$1
    case "$value" in
        /*) ;;
        *) return 1 ;;
    esac
    [ "$value" != / ] || return 1
    case "/$value/" in
        *[[:space:]]*|*'/../'*|*'/./'*) return 1 ;;
    esac
}

valid_size_value() {
    printf '%s\n' "$1" | awk '
        /^[0-9]+([.][0-9]+)?[A-Za-z]+$/ { valid = 1 }
        END { exit(!valid || NR != 1) }
    '
}

valid_unsigned_integer() {
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
    esac
}

suggest_lan_address() {
    if command -v ip >/dev/null 2>&1; then
        candidate=$(ip -4 route get 1.1.1.1 2>/dev/null \
            | awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}')
        if [ -n "$candidate" ] && valid_ipv4 "$candidate"; then
            printf '%s\n' "$candidate"
            return
        fi
    fi
    if command -v hostname >/dev/null 2>&1; then
        for candidate in $(hostname -I 2>/dev/null || true); do
            if valid_ipv4 "$candidate"; then
                printf '%s\n' "$candidate"
                return
            fi
        done
    fi
}

prompt_lan_address() {
    suggested=${1:-}
    [ -r /dev/tty ] && [ -w /dev/tty ] \
        || fail "no terminal is available; rerun with CURIO_LAN_ADDRESS=<LAN IPv4 address>"
    while true; do
        if [ -n "$suggested" ]; then
            printf 'Curio LAN IPv4 address [%s]: ' "$suggested" >/dev/tty
        else
            printf 'Curio LAN IPv4 address: ' >/dev/tty
        fi
        IFS= read -r reply </dev/tty \
            || fail "could not read the LAN address; set CURIO_LAN_ADDRESS and rerun"
        selected=${reply:-$suggested}
        if valid_ipv4 "$selected"; then
            printf '%s\n' "$selected"
            return
        fi
        echo "Enter a non-loopback IPv4 address such as 192.168.1.50." >/dev/tty
    done
}

read_env_value() {
    file=$1
    key=$2
    value=$(awk -v wanted="$key" '
        $0 ~ "^" wanted "=" { value = substr($0, length(wanted) + 2) }
        END { if (value == "") exit 1; sub(/\r$/, "", value); print value }
    ' "$file") || return 1
    case "$value" in
        \"*\") value=${value#\"}; value=${value%\"} ;;
        \'*\') value=${value#\'}; value=${value%\'} ;;
    esac
    printf '%s\n' "$value"
}

write_curio_env_if_missing() {
    path=$1
    lan_address=$2
    data_root=$3
    storage_max=$4
    redis_max=${5:-256mb}
    cleanup_threshold=${6:-2592000}
    [ ! -e "$path" ] || return 0

    tmp=$(mktemp "${path}.tmp.XXXXXX")
    chmod 0644 "$tmp"
    {
        echo "# Created by the Curio installer. This file is never overwritten on rerun."
        printf 'CURIO_LAN_ADDRESS=%s\n' "$lan_address"
        printf 'CURIO_DATA_ROOT=%s\n' "$data_root"
        printf 'CURIO_IPFS_STORAGE_MAX=%s\n' "$storage_max"
        printf 'CURIO_REDIS_MAX_MEMORY=%s\n' "$redis_max"
        printf 'CURIO_AR_IO_CACHE_CLEANUP_THRESHOLD=%s\n' "$cleanup_threshold"
    } >"$tmp"

    if [ -e "$path" ]; then
        rm -f -- "$tmp"
    else
        mv -- "$tmp" "$path"
    fi
}

ensure_directory() {
    path=$1
    mode=$2
    owner=$3
    group=$4
    if [ -e "$path" ]; then
        [ -d "$path" ] || fail "$path exists but is not a directory"
        return
    fi
    install -d -m "$mode" "$path"
    chown "$owner:$group" "$path"
}

directory_writable_by() {
    path=$1
    wanted_uid=$2
    wanted_gid=$3
    set -- $(stat -c '%u %g %a' "$path")
    owner=$1
    group=$2
    mode=$(($3 % 1000))
    owner_bits=$(((mode / 100) % 10))
    group_bits=$(((mode / 10) % 10))
    other_bits=$((mode % 10))
    if [ "$owner" -eq "$wanted_uid" ] && [ $((owner_bits & 2)) -ne 0 ]; then
        return 0
    fi
    if [ "$group" -eq "$wanted_gid" ] && [ $((group_bits & 2)) -ne 0 ]; then
        return 0
    fi
    [ $((other_bits & 2)) -ne 0 ]
}

ensure_runtime_directory() {
    path=$1
    mode=$2
    owner=$3
    group=$4
    label=$5
    if [ ! -e "$path" ]; then
        install -d -m "$mode" "$path"
        chown "$owner:$group" "$path"
        return
    fi
    [ -d "$path" ] || fail "$path exists but is not a directory"
    if [ -z "$(find "$path" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
        chown "$owner:$group" "$path"
        chmod "$mode" "$path"
    elif ! directory_writable_by "$path" "$owner" "$group"; then
        fail "existing $label directory $path is not writable by container UID:GID $owner:$group; ownership was not changed"
    fi
}

ensure_kubo_directory() {
    # Never recursively chown a repository containing identity, keys, pins, or
    # blocks. Only a pre-created, empty mount point can have its owner fixed.
    ensure_runtime_directory "$1" 0750 "$KUBO_UID" "$KUBO_GID" Kubo
}

write_state_file_if_missing() {
    path=$1
    mode=$2
    owner=$3
    group=$4
    content=$5
    if [ -e "$path" ]; then
        [ -f "$path" ] || fail "$path exists but is not a regular file"
        return
    fi
    tmp=$(mktemp "${path}.tmp.XXXXXX")
    printf '%s' "$content" >"$tmp"
    chmod "$mode" "$tmp"
    chown "$owner:$group" "$tmp"
    if [ -e "$path" ]; then
        rm -f -- "$tmp"
    else
        mv -- "$tmp" "$path"
    fi
}

prune_resolver_context() {
    source=$1
    destination=$2
    [ -d "$destination/src" ] || return 0

    # /opt/curio/resolver is an installed build context, not state. Remove
    # source files deleted by an update so an old module cannot leak into a
    # newly built wheel. The data tree under /var/lib/curio is never touched.
    find "$destination/src" -type f -print | while IFS= read -r path; do
        relative=${path#"$destination"/}
        [ -f "$source/$relative" ] || rm -f -- "$path"
    done
    find "$destination/src" -depth -type d -empty -delete
}

copy_resolver_context() {
    source=$1
    destination=$2
    prune_resolver_context "$source" "$destination"
    ensure_directory "$destination" 0755 0 0
    install -m 0644 "$source/.dockerignore" "$destination/.dockerignore"
    install -m 0644 "$source/Dockerfile" "$destination/Dockerfile"
    install -m 0644 "$source/pyproject.toml" "$destination/pyproject.toml"
    install -m 0644 "$source/constraints.txt" "$destination/constraints.txt"

    find "$source/src" -type d \( -name __pycache__ -o -name '*.egg-info' \) \
        -prune -o -type d -print | while IFS= read -r path; do
        relative=${path#"$source"/}
        ensure_directory "$destination/$relative" 0755 0 0
    done

    find "$source/src" -type d \( -name __pycache__ -o -name '*.egg-info' \) \
        -prune -o -type f ! -name '*.pyc' ! -name '*.pyo' -print \
        | while IFS= read -r path; do
            relative=${path#"$source"/}
            install -m 0644 "$path" "$destination/$relative"
        done
}

ensure_start_height() {
    state_file=$1
    if [ -e "$state_file" ]; then
        [ -f "$state_file" ] || fail "$state_file exists but is not a regular file"
        height=$(read_env_value "$state_file" START_HEIGHT || true)
        valid_unsigned_integer "$height" \
            || fail "$state_file does not contain a valid START_HEIGHT; repair it without deleting AR.IO state"
        echo "Keeping AR.IO first-deploy START_HEIGHT=$height"
        return
    fi

    echo "Resolving the current Arweave chain height (first install only)..."
    docker pull "$AR_IO_ENVOY_IMAGE"
    height=$(docker run --rm --entrypoint /bin/sh "$AR_IO_ENVOY_IMAGE" -c \
        'curl -fsSL --max-time 30 https://arweave.net/info | jq -er ".height | select(type == \"number\" and floor == .)"')
    valid_unsigned_integer "$height" \
        || fail "arweave.net returned an invalid chain height: ${height:-empty}"

    tmp=$(mktemp "${state_file}.tmp.XXXXXX")
    printf 'START_HEIGHT=%s\n' "$height" >"$tmp"
    chmod 0644 "$tmp"
    chown 0:0 "$tmp"
    if [ -e "$state_file" ]; then
        rm -f -- "$tmp"
    else
        mv -- "$tmp" "$state_file"
    fi
    echo "Recorded AR.IO first-deploy START_HEIGHT=$height"
}

compose() {
    docker compose --project-name curio --env-file "$CONFIG_FILE" --file "$COMPOSE_FILE" "$@"
}

wait_for_health() {
    operator=$1
    compose_file=$2
    env_file=$3
    timeout=${CURIO_HEALTH_TIMEOUT:-600}
    valid_unsigned_integer "$timeout" || fail "CURIO_HEALTH_TIMEOUT must be seconds"
    deadline=$(($(date +%s) + timeout))
    report=$(mktemp /tmp/curio-health.XXXXXX)

    echo "Waiting up to ${timeout}s for Curio health checks..."
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if CURIO_COMPOSE_FILE="$compose_file" CURIO_ENV_FILE="$env_file" \
            "$operator" health >"$report" 2>&1; then
            cat "$report"
            rm -f -- "$report"
            return 0
        fi
        sleep 5
    done

    echo "Curio did not become healthy within ${timeout}s. Component status:" >&2
    cat "$report" >&2
    rm -f -- "$report"
    return 1
}

main() {
    [ "$#" -eq 0 ] \
        || fail "this installer accepts no arguments; use CURIO_LAN_ADDRESS for unattended setup"
    [ "$(uname -s)" = Linux ] || fail "Curio can only be installed on Linux"
    [ "$(id -u)" -eq 0 ] \
        || fail "run this installer as root (for example: sudo ./appliance/install.sh)"
    command -v docker >/dev/null 2>&1 \
        || fail "Docker Engine is required but the docker command was not found; install Docker, then rerun"
    docker compose version >/dev/null 2>&1 \
        || fail "the Docker Compose plugin is required (the 'docker compose' command is unavailable)"
    docker info >/dev/null 2>&1 \
        || fail "cannot reach the Docker daemon; start Docker and rerun"

    script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)
    source_root=$(CDPATH= cd "$script_dir/.." && pwd)
    install_dir=/opt/curio
    config_dir=/etc/curio
    config_file="$config_dir/curio.env"
    bin_target=/usr/local/bin/curio

    for required in \
        "$script_dir/compose.yaml" \
        "$script_dir/curio" \
        "$script_dir/curio.env.example" \
        "$script_dir/kubo-init.sh" \
        "$source_root/resolver/.dockerignore" \
        "$source_root/resolver/Dockerfile" \
        "$source_root/resolver/constraints.txt" \
        "$source_root/resolver/pyproject.toml" \
        "$source_root/resolver/src"; do
        [ -e "$required" ] || fail "installation source is incomplete: missing $required"
    done

    CURRENT_STEP='creating installation and configuration directories'
    ensure_directory "$install_dir" 0755 0 0
    ensure_directory "$config_dir" 0755 0 0
    ensure_directory "$(dirname "$bin_target")" 0755 0 0

    CURRENT_STEP='validating or creating /etc/curio/curio.env'
    if [ -e "$config_file" ]; then
        [ -f "$config_file" ] || fail "$config_file exists but is not a regular file"
        echo "Keeping existing configuration: $config_file"
    else
        data_root=${CURIO_DATA_ROOT:-/var/lib/curio}
        valid_data_root "$data_root" \
            || fail "CURIO_DATA_ROOT must be an absolute path without whitespace or '..', and cannot be /"
        lan_address=${CURIO_LAN_ADDRESS:-}
        if [ -z "$lan_address" ]; then
            suggested=$(suggest_lan_address || true)
            lan_address=$(prompt_lan_address "$suggested")
        fi
        valid_ipv4 "$lan_address" \
            || fail "CURIO_LAN_ADDRESS must be a non-loopback IPv4 address (for example 192.168.1.50)"
        storage_max=${CURIO_IPFS_STORAGE_MAX:-20GB}
        redis_max=${CURIO_REDIS_MAX_MEMORY:-256mb}
        cleanup_threshold=${CURIO_AR_IO_CACHE_CLEANUP_THRESHOLD:-2592000}
        valid_size_value "$storage_max" \
            || fail "CURIO_IPFS_STORAGE_MAX must be a size such as 20GB"
        valid_size_value "$redis_max" \
            || fail "CURIO_REDIS_MAX_MEMORY must be a size such as 256mb"
        valid_unsigned_integer "$cleanup_threshold" \
            || fail "CURIO_AR_IO_CACHE_CLEANUP_THRESHOLD must be a number of seconds"
        write_curio_env_if_missing "$config_file" "$lan_address" "$data_root" \
            "$storage_max" "$redis_max" "$cleanup_threshold"
        echo "Created configuration: $config_file"
    fi

    lan_address=$(read_env_value "$config_file" CURIO_LAN_ADDRESS || true)
    valid_ipv4 "$lan_address" || fail "$config_file must contain a valid CURIO_LAN_ADDRESS"
    data_root=$(read_env_value "$config_file" CURIO_DATA_ROOT || printf '%s\n' /var/lib/curio)
    valid_data_root "$data_root" \
        || fail "CURIO_DATA_ROOT in $config_file must be an absolute path without whitespace or '..', and cannot be /"
    storage_max=$(read_env_value "$config_file" CURIO_IPFS_STORAGE_MAX || printf '%s\n' 20GB)
    redis_max=$(read_env_value "$config_file" CURIO_REDIS_MAX_MEMORY || printf '%s\n' 256mb)
    cleanup_threshold=$(read_env_value "$config_file" CURIO_AR_IO_CACHE_CLEANUP_THRESHOLD \
        || printf '%s\n' 2592000)
    valid_size_value "$storage_max" \
        || fail "CURIO_IPFS_STORAGE_MAX in $config_file must be a size such as 20GB"
    valid_size_value "$redis_max" \
        || fail "CURIO_REDIS_MAX_MEMORY in $config_file must be a size such as 256mb"
    valid_unsigned_integer "$cleanup_threshold" \
        || fail "CURIO_AR_IO_CACHE_CLEANUP_THRESHOLD in $config_file must be seconds"

    CURRENT_STEP="creating persistent state under $data_root"
    ensure_directory "$data_root" 0755 0 0
    ensure_kubo_directory "$data_root/ipfs"
    ensure_directory "$data_root/ar-io" 0755 0 0
    ensure_runtime_directory "$data_root/ar-io/redis" 0750 "$REDIS_UID" "$REDIS_GID" Redis
    ensure_directory "$data_root/ar-io/envoy-eds" 0755 0 0
    ensure_runtime_directory "$data_root/resolver" 0750 \
        "$RESOLVER_UID" "$RESOLVER_GID" resolver
    ensure_runtime_directory "$data_root/resolver/captures" 0750 \
        "$RESOLVER_UID" "$RESOLVER_GID" resolver-captures

    write_state_file_if_missing "$data_root/resolver/overrides.toml" 0640 \
        "$RESOLVER_UID" "$RESOLVER_GID" ''
    write_state_file_if_missing "$data_root/resolver/favorites.json" 0640 \
        "$RESOLVER_UID" "$RESOLVER_GID" '[]'
    write_state_file_if_missing "$data_root/resolver/captures/captures.jsonl" 0640 \
        "$RESOLVER_UID" "$RESOLVER_GID" ''
    write_state_file_if_missing "$data_root/resolver/captures/warmed.jsonl" 0640 \
        "$RESOLVER_UID" "$RESOLVER_GID" ''

    CURRENT_STEP='recording AR.IO first-deploy chain height'
    ensure_start_height "$data_root/ar-io/start-height.env"

    CURRENT_STEP='installing application files under /opt/curio'
    install -m 0644 "$script_dir/compose.yaml" "$install_dir/compose.yaml"
    install -m 0644 "$script_dir/curio.env.example" "$install_dir/curio.env.example"
    install -m 0755 "$script_dir/kubo-init.sh" "$install_dir/kubo-init.sh"
    install -m 0755 "$script_dir/curio" "$bin_target"
    copy_resolver_context "$source_root/resolver" "$install_dir/resolver"

    version=$(awk -F '"' '/^version = / { print $2; exit }' "$source_root/resolver/pyproject.toml")
    [ -n "$version" ] || fail "could not determine the resolver version"
    printf '%s\n' "$version" >"$install_dir/VERSION"
    chmod 0644 "$install_dir/VERSION"

    CONFIG_FILE=$config_file
    COMPOSE_FILE=$install_dir/compose.yaml
    CURRENT_STEP='rendering the installed Compose project'
    compose config --quiet

    CURRENT_STEP='pulling pinned upstream images'
    echo "Pulling pinned upstream images..."
    compose pull --policy missing kubo ar-io-redis ar-io-core ar-io-observer ar-io-envoy
    CURRENT_STEP='building the Curio resolver image'
    echo "Building the Curio resolver image..."
    compose build resolver
    CURRENT_STEP='starting the Curio Compose project'
    echo "Starting Curio..."
    compose up --detach --no-build

    CURRENT_STEP='waiting for Curio health checks'
    wait_for_health "$bin_target" "$install_dir/compose.yaml" "$config_file"

    INSTALL_SUCCEEDED=1
    cat <<EOF

Curio is installed and healthy.

  AR.IO gateway:  http://$lan_address:3000
  IPFS gateway:   http://$lan_address:8080
  Curio resolver: http://$lan_address:8090

Operator commands:
  curio status
  curio health
  curio logs resolver --follow
  curio restart [service]
  curio stop
  curio start
EOF
}

if [ "${CURIO_INSTALL_SH_SOURCE_ONLY:-0}" != 1 ]; then
    trap on_exit EXIT
    trap 'exit 1' HUP INT TERM
    main "$@"
fi

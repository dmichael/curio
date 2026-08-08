#!/bin/sh
# Per-user Curio installer. It deliberately needs only a Docker runtime the
# invoking user can access; it never mutates privileged host locations.
set -eu

fail() { echo "curio install: $*" >&2; exit 1; }

main() {
    [ "$(uname -s)" = Linux ] || fail "Curio can only be installed on Linux"
    command -v docker >/dev/null 2>&1 || fail "a user-accessible Docker runtime is required"
    docker compose version >/dev/null 2>&1 || fail "Docker Compose plugin is required"
    docker info >/dev/null 2>&1 || fail "cannot access Docker as this user"

    script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)
    source_root=$(CDPATH= cd "$script_dir/.." && pwd)
    data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
    config_home=${XDG_CONFIG_HOME:-"$HOME/.config"}
    bin_home=${XDG_BIN_HOME:-"$HOME/.local/bin"}
    app_root=${CURIO_APP_ROOT:-"$data_home/curio/app"}
    data_root=${CURIO_DATA_ROOT:-"$data_home/curio/state"}
    config_dir="$config_home/curio"
    config_file="$config_dir/curio.env"

    umask 077
    mkdir -p "$app_root" "$data_root/ipfs" "$data_root/ar-io/redis" \
        "$data_root/ar-io/envoy-eds" "$data_root/media" "$config_dir" "$bin_home"
    chmod 700 "$data_root" "$config_dir"
    if [ ! -f "$config_file" ]; then
        token=${CURIO_CURATOR_TOKEN:-$(dd if=/dev/urandom bs=24 count=1 2>/dev/null | base64 | tr -d '\n')}
        cat >"$config_file" <<EOF
CURIO_APP_ROOT=$app_root
CURIO_DATA_ROOT=$data_root
CURIO_HOST_UID=$(id -u)
CURIO_HOST_GID=$(id -g)
CURIO_CURATOR_TOKEN=$token
CURIO_IPFS_STORAGE_MAX=${CURIO_IPFS_STORAGE_MAX:-20GB}
CURIO_REDIS_MAX_MEMORY=${CURIO_REDIS_MAX_MEMORY:-256mb}
CURIO_AR_IO_CACHE_CLEANUP_THRESHOLD=${CURIO_AR_IO_CACHE_CLEANUP_THRESHOLD:-2592000}
CURIO_PORT=${CURIO_PORT:-8090}
EOF
        chmod 600 "$config_file"
    fi
    grep -q '^CURIO_CURATOR_TOKEN=.' "$config_file" || fail "CURIO_CURATOR_TOKEN is required"
    grep -q '^CURIO_HOST_UID=[0-9][0-9]*$' "$config_file" || fail "CURIO_HOST_UID is required"
    grep -q '^CURIO_HOST_GID=[0-9][0-9]*$' "$config_file" || fail "CURIO_HOST_GID is required"
    [ -f "$data_root/ar-io/start-height.env" ] || printf 'START_HEIGHT=%s\n' "${CURIO_AR_IO_START_HEIGHT:-0}" >"$data_root/ar-io/start-height.env"

    install -m 0644 "$script_dir/compose.yaml" "$app_root/compose.yaml"
    install -m 0755 "$script_dir/kubo-init.sh" "$app_root/kubo-init.sh"
    rm -rf "$app_root/resolver"
    mkdir -p "$app_root/resolver"
    cp -R "$source_root/resolver/." "$app_root/resolver/"
    install -m 0755 "$script_dir/curio" "$bin_home/curio"
    install -m 0755 "$source_root/install.sh" "$app_root/install.sh"
    version=$(awk -F '"' '/^version = / {print $2; exit}' "$source_root/resolver/pyproject.toml")
    [ -n "$version" ] || fail "cannot determine package version"
    printf '%s\n' "$version" >"$app_root/VERSION"

    docker compose --project-name curio --env-file "$config_file" --file "$app_root/compose.yaml" config --quiet
    docker compose --project-name curio --env-file "$config_file" --file "$app_root/compose.yaml" build resolver
    docker compose --project-name curio --env-file "$config_file" --file "$app_root/compose.yaml" up -d
    echo "Curio $version installed. Add $bin_home to PATH, then run: curio status"
}
main "$@"

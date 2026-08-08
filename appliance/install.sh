#!/bin/sh
# Per-user, no-sudo installer. Application releases are immutable directories;
# `current` is switched only after a complete release has been staged.
set -eu

fail() { echo "curio install: $*" >&2; exit 1; }
# This touches only an installer-created, non-symlink staging directory; it is
# never an app/data root supplied by the caller.
cleanup_stage() { [ -d "$1" ] && [ ! -L "$1" ] && find "$1" -depth -delete; }
read_env() { awk -F= -v k="$2" '$1 == k {sub(/^[^=]*=/, ""); print; exit}' "$1"; }
valid_root() {
    case "$1" in /*) ;; *) return 1;; esac
    [ "$1" != / ] || return 1
    case "/$1/" in *'/../'*|*'/./'*|*[[:space:]]*) return 1;; esac
}

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
    config_dir="$config_home/curio"
    config_file="$config_dir/curio.env"
    requested_app_root=${CURIO_APP_ROOT:-"$data_home/curio/app"}
    requested_data_root=${CURIO_DATA_ROOT:-"$data_home/curio/state"}
    valid_root "$requested_app_root" || fail "CURIO_APP_ROOT must be a safe absolute non-root path"
    valid_root "$requested_data_root" || fail "CURIO_DATA_ROOT must be a safe absolute non-root path"

    umask 077
    mkdir -p "$config_dir" "$bin_home"
    chmod 700 "$config_dir"
    if [ -e "$config_file" ]; then
        [ -f "$config_file" ] || fail "$config_file is not a regular file"
        app_root=$(read_env "$config_file" CURIO_APP_ROOT)
        data_root=$(read_env "$config_file" CURIO_DATA_ROOT)
        [ -n "$app_root" ] && [ -n "$data_root" ] || fail "$config_file lacks CURIO_APP_ROOT or CURIO_DATA_ROOT"
        if [ -n "${CURIO_APP_ROOT:-}" ] && [ "$requested_app_root" != "$app_root" ]; then
            fail "CURIO_APP_ROOT differs from existing configuration; make an explicit migration instead"
        fi
    else
        app_root=$requested_app_root
        data_root=$requested_data_root
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
CURIO_PUBLIC_BASE_URL=${CURIO_PUBLIC_BASE_URL:-}
EOF
        chmod 600 "$config_file"
    fi
    valid_root "$app_root" && valid_root "$data_root" || fail "configured roots are unsafe"
    grep -q '^CURIO_CURATOR_TOKEN=.' "$config_file" || fail "CURIO_CURATOR_TOKEN is required"
    grep -q '^CURIO_HOST_UID=[0-9][0-9]*$' "$config_file" || fail "CURIO_HOST_UID is required"
    grep -q '^CURIO_HOST_GID=[0-9][0-9]*$' "$config_file" || fail "CURIO_HOST_GID is required"

    mkdir -p "$app_root/releases" "$data_root/ipfs" "$data_root/ar-io/redis" \
        "$data_root/ar-io/envoy-eds" "$data_root/media"
    chmod 700 "$data_root"
    [ -f "$data_root/ar-io/start-height.env" ] || printf 'START_HEIGHT=%s\n' "${CURIO_AR_IO_START_HEIGHT:-0}" >"$data_root/ar-io/start-height.env"

    version=$(awk -F '"' '/^version = / {print $2; exit}' "$source_root/resolver/pyproject.toml")
    [ -n "$version" ] || fail "cannot determine package version"
    release="releases/$version-$(date +%s)-$$"
    stage="$app_root/.stage-$$"
    trap 'cleanup_stage "$stage"' EXIT HUP INT TERM
    mkdir "$stage"
    install -m 0644 "$script_dir/compose.yaml" "$stage/compose.yaml"
    install -m 0755 "$script_dir/kubo-init.sh" "$stage/kubo-init.sh"
    install -m 0755 "$script_dir/curio" "$stage/curio"
    install -m 0755 "$source_root/install.sh" "$stage/install.sh"
    cp -R "$source_root/resolver" "$stage/resolver"
    printf '%s\n' "$version" >"$stage/VERSION"
    mv "$stage" "$app_root/$release"
    trap - EXIT HUP INT TERM
    # Reject a malicious/non-symlink current rather than deleting caller data.
    [ ! -e "$app_root/current" ] || [ -L "$app_root/current" ] || fail "$app_root/current is not an installer-managed symlink"
    ln -s "$release" "$app_root/.current-$$"
    mv -f "$app_root/.current-$$" "$app_root/current"
    install -m 0755 "$script_dir/curio" "$bin_home/curio"

    docker compose --project-name curio --env-file "$config_file" --file "$app_root/current/compose.yaml" config --quiet
    docker compose --project-name curio --env-file "$config_file" --file "$app_root/current/compose.yaml" build resolver
    docker compose --project-name curio --env-file "$config_file" --file "$app_root/current/compose.yaml" up -d
    echo "Curio $version installed. Add $bin_home to PATH, then run: curio status"
}
if [ "${CURIO_INSTALL_SH_SOURCE_ONLY:-0}" != 1 ]; then
    main "$@"
fi

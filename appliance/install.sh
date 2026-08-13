#!/bin/sh
# Per-user, no-sudo installer. Releases are immutable and `current` changes
# only after the new release has started and passed its complete health check.
set -eu

AR_IO_CORE_IMAGE='ghcr.io/ar-io/ar-io-core:f3032933c6039305bc5ecec0d486526c6d60d6ea@sha256:1155d2042870e6ba2c1457258765c16b762fe66026e4a42799abfca3b5d0d900'
fail() { echo "curio install: $*" >&2; exit 1; }
cleanup_stage() { [ -d "$1" ] && [ ! -L "$1" ] && find "$1" -depth -delete; }
read_env() { awk -F= -v k="$2" '$1 == k {sub(/^[^=]*=/, ""); print; exit}' "$1"; }
valid_root() { case "$1" in /*) ;; *) return 1;; esac; [ "$1" != / ] || return 1; case "/$1/" in *'/../'*|*'/./'*|*[[:space:]]*) return 1;; esac; }
valid_uint() { case "$1" in ''|*[!0-9]*) return 1;; esac; }

# Linux is the supported target. -T is important: plain mv follows a
# destination directory symlink and nests .current inside its old release.
switch_current() {
    release=$1
    ln -s "$release" "$app_root/.current-$$"
    mv -Tf "$app_root/.current-$$" "$app_root/current"
}

ensure_start_height() {
    state_file=$1
    if [ -e "$state_file" ]; then
        [ -f "$state_file" ] || fail "$state_file is not a regular file"
        height=$(read_env "$state_file" START_HEIGHT || true)
        valid_uint "$height" || fail "$state_file lacks a valid START_HEIGHT; repair it without deleting AR.IO state"
        echo "Keeping AR.IO first-deploy START_HEIGHT=$height"
        return
    fi
    echo "Resolving the current Arweave chain height (first install only)..."
    docker pull "$AR_IO_CORE_IMAGE"
    height=$(docker run --rm --entrypoint /nodejs/bin/node "$AR_IO_CORE_IMAGE" -e \
        'const https=require("https");const r=https.get("https://arweave.net/info",{timeout:30000},x=>{let b="";x.on("data",c=>b+=c);x.on("end",()=>{try{const h=JSON.parse(b).height;if(Number.isSafeInteger(h)&&h>=0)console.log(h);else process.exitCode=1}catch{process.exitCode=1}})});r.on("timeout",()=>r.destroy(new Error("timeout")));r.on("error",()=>process.exitCode=1);') || fail "could not query Arweave height through pinned AR.IO Core image"
    valid_uint "$height" || fail "arweave.net returned an invalid chain height: ${height:-empty}"
    temporary="$state_file.tmp.$$"
    (umask 077; printf 'START_HEIGHT=%s\n' "$height" >"$temporary")
    # State is per-user: do not make an installer-created root-owned file.
    mv -f "$temporary" "$state_file"
    echo "Recorded AR.IO first-deploy START_HEIGHT=$height"
}

assert_state_owned() {
    foreign=$(find "$data_root" -xdev \( ! -user "$(id -u)" -o ! -group "$(id -g)" \) -print -quit 2>/dev/null || true)
    [ -z "$foreign" ] || { echo "persistent state is not owned by the installing user: $foreign (fix ownership before rerunning)" >&2; return 1; }
}

rollback() {
    reason=$1
    echo "curio install: $reason" >&2
    echo "curio install: new deployment diagnostics follow:" >&2
    docker compose --project-name curio --env-file "$config_file" --file "$new_compose" ps --all >&2 || true
    docker compose --project-name curio --env-file "$config_file" --file "$new_compose" logs --no-color >&2 || true
    # A failed first deployment must not leave a running partial project;
    # likewise stop the failed new graph before restoring the old release.
    docker compose --project-name curio --env-file "$config_file" --file "$new_compose" down --remove-orphans >&2 || true
    if [ -n "$prior_release" ]; then
        echo "curio install: restoring previous release $prior_release" >&2
        switch_current "$prior_release"
        docker compose --project-name curio --env-file "$config_file" --file "$app_root/current/compose.yaml" up -d --wait --wait-timeout "$health_timeout" >&2 || \
            echo "curio install: previous deployment could not be restored automatically; diagnostics above were preserved" >&2
    else
        # Do not report a failed first deploy as installed.
        rm -f "$app_root/current"
    fi
    exit 1
}

main() {
    [ "$(uname -s)" = Linux ] || fail "Curio can only be installed on Linux"
    command -v docker >/dev/null 2>&1 || fail "a user-accessible Docker runtime is required"
    script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)
    source_root=$(CDPATH= cd "$script_dir/.." && pwd)
    # Compose stats the caller's working directory during validation. Run from
    # / so an inaccessible cwd (for example root's home under sudo -u) cannot
    # fail the install or its rollback. Every path below is absolute.
    cd /
    docker compose version >/dev/null 2>&1 || fail "Docker Compose plugin is required"
    docker info >/dev/null 2>&1 || fail "cannot access Docker as this user"
    data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}; config_home=${XDG_CONFIG_HOME:-"$HOME/.config"}; bin_home=${XDG_BIN_HOME:-"$HOME/.local/bin"}
    # An explicitly configured environment file (as the curio wrapper passes
    # during update) must be reused, never silently replaced by a fresh
    # default-location install.
    config_file=${CURIO_ENV_FILE:-"$config_home/curio/curio.env"}
    config_dir=$(dirname "$config_file")
    [ -z "${CURIO_ENV_FILE:-}" ] || [ -f "$config_file" ] || fail "CURIO_ENV_FILE is set but $config_file does not exist"
    requested_app_root=${CURIO_APP_ROOT:-"$data_home/curio/app"}; requested_data_root=${CURIO_DATA_ROOT:-"$data_home/curio/state"}
    valid_root "$requested_app_root" || fail "CURIO_APP_ROOT must be a safe absolute non-root path"
    valid_root "$requested_data_root" || fail "CURIO_DATA_ROOT must be a safe absolute non-root path"
    umask 077; mkdir -p "$config_dir" "$bin_home"; chmod 700 "$config_dir"
    if [ -e "$config_file" ]; then
        [ -f "$config_file" ] || fail "$config_file is not a regular file"
        app_root=$(read_env "$config_file" CURIO_APP_ROOT); data_root=$(read_env "$config_file" CURIO_DATA_ROOT)
        [ -n "$app_root" ] && [ -n "$data_root" ] || fail "$config_file lacks CURIO_APP_ROOT or CURIO_DATA_ROOT"
        [ -z "${CURIO_APP_ROOT:-}" ] || [ "$requested_app_root" = "$app_root" ] || fail "CURIO_APP_ROOT differs from existing configuration; make an explicit migration instead"
        [ -z "${CURIO_DATA_ROOT:-}" ] || [ "$requested_data_root" = "$data_root" ] || fail "CURIO_DATA_ROOT differs from existing configuration; make an explicit migration instead"
    else
        app_root=$requested_app_root; data_root=$requested_data_root
        cat >"$config_file" <<EOF
CURIO_APP_ROOT=$app_root
CURIO_DATA_ROOT=$data_root
CURIO_HOST_UID=$(id -u)
CURIO_HOST_GID=$(id -g)
CURIO_IPFS_STORAGE_MAX=${CURIO_IPFS_STORAGE_MAX:-20GB}
CURIO_STATIC_CACHE_MAX_BYTES=${CURIO_STATIC_CACHE_MAX_BYTES:-1000000000}
CURIO_ARWEAVE_COLD_TIMEOUT=${CURIO_ARWEAVE_COLD_TIMEOUT:-300}
CURIO_PORT=${CURIO_PORT:-8090}
CURIO_PUBLIC_BASE_URL=${CURIO_PUBLIC_BASE_URL:-}
CURIO_TRUSTED_PROXY_CIDRS=${CURIO_TRUSTED_PROXY_CIDRS:-}
EOF
        chmod 600 "$config_file"
    fi
    valid_root "$app_root" && valid_root "$data_root" || fail "configured roots are unsafe"
    grep -q '^CURIO_HOST_UID=[0-9][0-9]*$' "$config_file" || fail "CURIO_HOST_UID is required"
    grep -q '^CURIO_HOST_GID=[0-9][0-9]*$' "$config_file" || fail "CURIO_HOST_GID is required"
    # Historical Redis/Envoy/retained directories are intentionally untouched.
    mkdir -p "$app_root/releases" "$data_root/ipfs" "$data_root/ar-io" "$data_root/media"
    chmod 700 "$data_root"; assert_state_owned || fail "persistent state ownership check failed"
    ensure_start_height "$data_root/ar-io/start-height.env"
    assert_state_owned || fail "persistent state ownership check failed"

    version=$(awk -F '"' '/^version = / {print $2; exit}' "$source_root/resolver/pyproject.toml")
    [ -n "$version" ] || fail "cannot determine package version"
    release="releases/$version-$(date +%s)-$$"; stage="$app_root/.stage-$$"
    trap 'cleanup_stage "$stage"' EXIT HUP INT TERM
    mkdir "$stage"; install -m 0644 "$script_dir/compose.yaml" "$stage/compose.yaml"; install -m 0755 "$script_dir/kubo-init.sh" "$stage/kubo-init.sh"; install -m 0755 "$script_dir/curio" "$stage/curio"; install -m 0755 "$source_root/install.sh" "$stage/install.sh"
    cp -R "$source_root/resolver" "$stage/resolver"; printf '%s\n' "$version" >"$stage/VERSION"
    mv "$stage" "$app_root/$release"; trap - EXIT HUP INT TERM
    prior_release=
    if [ -e "$app_root/current" ] || [ -L "$app_root/current" ]; then
        [ -L "$app_root/current" ] || fail "$app_root/current is not an installer-managed symlink"
        prior_release=$(readlink "$app_root/current")
        case "$prior_release" in releases/*) ;; *) fail "$app_root/current has an unsafe target";; esac
    fi
    switch_current "$release"
    new_compose="$app_root/current/compose.yaml"
    health_timeout=${CURIO_HEALTH_TIMEOUT:-600}; valid_uint "$health_timeout" || rollback "CURIO_HEALTH_TIMEOUT must be seconds"
    docker compose --project-name curio --env-file "$config_file" --file "$new_compose" config --quiet || rollback "Compose configuration failed"
    docker compose --project-name curio --env-file "$config_file" --file "$new_compose" build resolver || rollback "resolver image build failed"
    echo "Waiting up to ${health_timeout}s for all Curio services..."
    # Remove services absent from this release (for example, the former
    # Redis/Envoy/retained graph). Rollback below starts the prior graph again.
    docker compose --project-name curio --env-file "$config_file" --file "$new_compose" up -d --wait --wait-timeout "$health_timeout" --remove-orphans || rollback "Compose start or health check failed"
    assert_state_owned || rollback "persistent state ownership check failed after start"
    install -m 0755 "$script_dir/curio" "$bin_home/curio"
    port=$(read_env "$config_file" CURIO_PORT); [ -n "$port" ] || port=8090
    echo "Curio $version installed and healthy. Add $bin_home to PATH, then run: curio status"
    echo "Agent access (MCP): http://<host>:$port/mcp"
}
[ "${CURIO_INSTALL_SH_SOURCE_ONLY:-0}" = 1 ] || main "$@"

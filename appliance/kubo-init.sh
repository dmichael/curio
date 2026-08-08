#!/bin/sh
# Runs from the official Kubo image's /container-init.d before the daemon.
# It manages only Curio-owned settings; identity, keys, pins, and data remain
# untouched.
set -eu

set_config() {
    key=$1
    desired_json=$2
    desired_scalar=$desired_json
    case "$desired_scalar" in
        \"*\")
            desired_scalar=${desired_scalar#\"}
            desired_scalar=${desired_scalar%\"}
            ;;
    esac

    current=$(ipfs config --json "$key" 2>/dev/null || printf '%s' '__missing__')
    if [ "$current" != "$desired_json" ] && [ "$current" != "$desired_scalar" ]; then
        echo "Setting Kubo $key"
        ipfs config --json "$key" "$desired_json"
    fi
}

storage_max=${CURIO_IPFS_STORAGE_MAX:-20GB}
case "$storage_max" in
    *[!A-Za-z0-9.]*|'')
        echo "Invalid CURIO_IPFS_STORAGE_MAX: $storage_max" >&2
        exit 1
        ;;
esac

set_config Addresses.API '"/ip4/0.0.0.0/tcp/5001"'
set_config Addresses.Gateway '"/ip4/0.0.0.0/tcp/8080"'
set_config Datastore.StorageMax "\"$storage_max\""
set_config Swarm.ConnMgr.LowWater '20'
set_config Swarm.ConnMgr.HighWater '50'
set_config Routing.Type '"autoclient"'

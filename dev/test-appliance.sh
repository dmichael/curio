#!/usr/bin/env bash
# Disposable-host smoke test helper. Run from a Linux user with Docker access.
set -Eeuo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
"$ROOT/appliance/install.sh"
curio health

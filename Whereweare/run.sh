#!/usr/bin/env bash
set -euo pipefail
hub_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$hub_root/server.py" "$@"

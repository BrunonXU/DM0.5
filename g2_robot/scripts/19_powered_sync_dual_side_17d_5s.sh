#!/usr/bin/env bash
# 兼容旧文件名；实际入口已不再固定5秒。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/19_powered_sync_dual_side_17d.sh" "$@"

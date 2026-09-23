#!/usr/bin/env bash
# 兼容旧入口：正式Sync已统一回05和原端口7892，本文件不再维护独立配置。
# 输入与输出均与05完全一致。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

echo "提示：13是兼容别名；正式入口统一为05_powered_sync.sh（7892）。"
exec "$SCRIPT_DIR/05_powered_sync.sh" "$@"

#!/usr/bin/env bash
# 用途：收集最近24小时的G2结构化日志、诊断产物与源码校验值。
# 输入：公共环境变量，无位置参数。
# 输出：logs/bundles/下tar.gz及其SHA256，不修改原始日志。
# 方法：按文件类型复制保留相对路径，再生成环境信息和源码清单。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

load_g2_environment
STAMP="$(timestamp)"
BUNDLE_DIR="$G2_BUNDLE_LOG_DIR/g2_runtime_${STAMP}"
ARCHIVE="${BUNDLE_DIR}.tar.gz"
mkdir -p "$BUNDLE_DIR"

{
  echo "generated_at=$STAMP"
  echo "host=$(hostname)"
  echo "project=$G2_PROJECT_DIR"
  echo "endpoint=$POLICY_ENDPOINT"
  uname -a
  ip -brief address
  ip route
  pgrep -af 'g2_sort.deployment|g2_sort.operations' || true
} > "$BUNDLE_DIR/environment.txt"

(
  cd "$G2_PROJECT_DIR"
  find logs -maxdepth 4 -type f \
    \( -name '*.jsonl' -o -name '*.log' -o -name '*.jpg' -o -name 'preflight.json' \) \
    -newermt '24 hours ago' \
    -exec cp --parents -- '{}' "$BUNDLE_DIR" ';'
)

find g2_sort scripts docs -type f \
  \( -name '*.py' -o -name '*.sh' -o -name '*.md' -o -name '*.json' \) \
  -print0 | sort -z | xargs -0 sha256sum > "$BUNDLE_DIR/source_sha256.txt"

tar -czf "$ARCHIVE" -C "$(dirname "$BUNDLE_DIR")" "$(basename "$BUNDLE_DIR")"
sha256sum "$ARCHIVE" > "${ARCHIVE}.sha256"
echo "bundle=$ARCHIVE"
cat "${ARCHIVE}.sha256"

#!/usr/bin/env bash
# 只读拉取 G2 /data/record 下按修改时间排序的最新 N 条记录。
# 默认拉取最新两条到当前任务 data/，拒绝覆盖本地同名路径。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TASK_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
LOCAL_ROOT="$TASK_ROOT/data"
G2_HOST="${G2_HOST:-agi@192.0.2.8}"
REMOTE_ROOT="${G2_RECORD_ROOT:-/data/record}"
COUNT="${1:-2}"

if ! [[ "$COUNT" =~ ^[1-9][0-9]*$ ]]; then
  echo "STOP: 数量必须是正整数，当前值：$COUNT" >&2
  exit 2
fi

mkdir -p "$LOCAL_ROOT"

mapfile -t latest_records < <(
  ssh "$G2_HOST" \
    "find '$REMOTE_ROOT' -mindepth 1 -maxdepth 1 -printf '%T@\t%f\n' | sort -nr | head -n '$COUNT' | cut -f2-"
)

if (( ${#latest_records[@]} != COUNT )); then
  echo "STOP: 远端只返回 ${#latest_records[@]} 条，期望 $COUNT 条。" >&2
  exit 1
fi

echo "将从 $G2_HOST:$REMOTE_ROOT 拉取："
for record in "${latest_records[@]}"; do
  if ! [[ "$record" =~ ^[A-Za-z0-9._-]+$ ]] || [[ "$record" == "." || "$record" == ".." ]]; then
    echo "STOP: 不安全或不支持的记录名：$record" >&2
    exit 1
  fi
  if [[ -e "$LOCAL_ROOT/$record" ]]; then
    echo "STOP: 本地已存在，拒绝覆盖：$LOCAL_ROOT/$record" >&2
    exit 1
  fi
  echo "  $record"
done

for record in "${latest_records[@]}"; do
  scp -rp -- "$G2_HOST:$REMOTE_ROOT/$record" "$LOCAL_ROOT/"
done

echo "完成。本地记录："
for record in "${latest_records[@]}"; do
  du -sh -- "$LOCAL_ROOT/$record"
done


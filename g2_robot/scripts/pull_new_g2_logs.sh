#!/usr/bin/env bash
# 用途：在6000D递归拉取G2上本地尚不存在的日志，保留logs相对目录结构。
# 输入：固定只读来源agi@192.0.2.8:/home/user/xza/DM0.5/logs/；可选
# --include-gdk-native用于底层故障排查。
# 输出：当前仓库logs/下新增文件；默认跳过大体积GDK native日志，同名文件不覆盖。
# 方法：优先rsync --ignore-existing；任一端无rsync时退回ssh find +逐文件scp。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
LOCAL_LOG_ROOT="$REPO_ROOT/logs"
G2_HOST="agi@192.0.2.8"
G2_LOG_ROOT="/home/user/xza/DM0.5/logs"
include_gdk_native=false

case "${1:-}" in
  "") ;;
  --include-gdk-native) include_gdk_native=true ;;
  -h|--help)
    echo "usage: $0 [--include-gdk-native]"
    exit 0
    ;;
  *)
    echo "STOP: 未知参数：$1" >&2
    echo "usage: $0 [--include-gdk-native]" >&2
    exit 2
    ;;
esac
if (( $# > 1 )); then
  echo "STOP: 参数过多" >&2
  exit 2
fi

mkdir -p "$LOCAL_LOG_ROOT"

if command -v rsync >/dev/null 2>&1 \
  && ssh "$G2_HOST" 'command -v rsync >/dev/null 2>&1'; then
  echo "使用rsync只拉取本地未见过的G2日志；已有文件不会覆盖。"
  if [[ "$include_gdk_native" == true ]]; then
    rsync -rtv --ignore-existing \
      --exclude '*.partial' \
      "$G2_HOST:$G2_LOG_ROOT/" \
      "$LOCAL_LOG_ROOT/"
  else
    echo "默认跳过logs/gdk_native；底层故障时使用--include-gdk-native。"
    rsync -rtv --ignore-existing --exclude '/gdk_native/' \
      --exclude '*.partial' \
      "$G2_HOST:$G2_LOG_ROOT/" \
      "$LOCAL_LOG_ROOT/"
  fi
  exit 0
fi

echo "任一端没有rsync，使用ssh只读列举 + scp逐文件回退。"
added=0
skipped=0
while IFS= read -r remote_path; do
  [[ "$remote_path" == *.partial ]] && continue
  case "$remote_path" in
    "$G2_LOG_ROOT"/*) ;;
    *)
      echo "STOP: G2返回了logs目录之外的路径：$remote_path" >&2
      exit 1
      ;;
  esac
  relative_path="${remote_path#"$G2_LOG_ROOT"/}"
  local_path="$LOCAL_LOG_ROOT/$relative_path"
  if [[ -e "$local_path" ]]; then
    skipped=$((skipped + 1))
    continue
  fi
  local_dir="$(dirname -- "$local_path")"
  mkdir -p "$local_dir"
  temporary="$(mktemp "$local_dir/.g2-log-pull.XXXXXX")"
  if scp -p -- "$G2_HOST:$remote_path" "$temporary"; then
    mv -- "$temporary" "$local_path"
    echo "新增：logs/$relative_path"
    added=$((added + 1))
  else
    rm -f -- "$temporary"
    echo "STOP: 拉取失败：$remote_path" >&2
    exit 1
  fi
done < <(
  if [[ "$include_gdk_native" == true ]]; then
    ssh "$G2_HOST" find "$G2_LOG_ROOT" -type f -print
  else
    echo "默认跳过logs/gdk_native；底层故障时使用--include-gdk-native。" >&2
    ssh "$G2_HOST" find "$G2_LOG_ROOT" \
      -path "$G2_LOG_ROOT/gdk_native" -prune -o -type f -print
  fi
)

echo "完成：新增${added}个，跳过本地已有${skipped}个；没有覆盖任何旧日志。"

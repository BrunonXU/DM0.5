#!/usr/bin/env bash
# Pull new G2 logs and atomically refresh the local static motion dashboard.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
pull=true
args=()

while (( $# > 0 )); do
  case "$1" in
    --no-pull) pull=false; shift ;;
    --all) args+=(--all); shift ;;
    --run)
      (( $# >= 2 )) || { echo "STOP: --run缺少日志路径" >&2; exit 2; }
      args+=(--run "$2"); shift 2 ;;
    -h|--help)
      echo "usage: $0 [--no-pull] [--all] [--run LOG.jsonl]"
      exit 0 ;;
    *) echo "STOP: 未知参数：$1" >&2; exit 2 ;;
  esac
done

if [[ "$pull" == true ]]; then
  "$SCRIPT_DIR/pull_new_g2_logs.sh"
fi

cd "$REPO_ROOT"
python3 -m g2_sort.validation.build_motion_dashboard \
  --log-root "$REPO_ROOT/logs" \
  --output "$REPO_ROOT/logs/analysis/motion_dashboard" \
  "${args[@]}"

echo "浏览命令：$SCRIPT_DIR/serve_motion_dashboard.sh --port 8765"

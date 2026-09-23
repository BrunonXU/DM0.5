#!/usr/bin/env bash
# Serve the generated dashboard on localhost; use IDE port forwarding remotely.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
port=8765
bind=127.0.0.1
watch=true

while (( $# > 0 )); do
  case "$1" in
    --port) (( $# >= 2 )) || { echo "STOP: --port缺少数值" >&2; exit 2; }; port="$2"; shift 2 ;;
    --bind) (( $# >= 2 )) || { echo "STOP: --bind缺少地址" >&2; exit 2; }; bind="$2"; shift 2 ;;
    --watch) watch=true; shift ;;
    --no-watch) watch=false; shift ;;
    -h|--help) echo "usage: $0 [--port 8765] [--bind 127.0.0.1] [--watch|--no-watch]"; exit 0 ;;
    *) echo "STOP: 未知参数：$1" >&2; exit 2 ;;
  esac
done

[[ "$port" =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 )) || {
  echo "STOP: --port必须是1..65535" >&2
  exit 2
}

dashboard="$REPO_ROOT/logs/analysis/motion_dashboard"
if [[ ! -f "$dashboard/index.html" ]]; then
  echo "尚未生成仪表盘，先执行一次本地构建。"
  cd "$REPO_ROOT"
  PYTHONPATH="$REPO_ROOT" python3 -m g2_sort.validation.build_motion_dashboard \
    --log-root "$REPO_ROOT/logs" \
    --output "$dashboard"
fi

watch_pid=""
dashboard_fingerprint() {
  {
    stat -c '%Y %s %n' \
      "$REPO_ROOT/g2_sort/validation/build_motion_dashboard.py"
    if [[ -d "$REPO_ROOT/logs/telemetry" ]]; then
      find "$REPO_ROOT/logs/telemetry" -type f -name '*.jsonl' \
        -printf '%T@ %s %p\n' | sort
    fi
  } | sha256sum | awk '{print $1}'
}

watch_dashboard() {
  local previous current
  previous="$(dashboard_fingerprint)"
  while true; do
    sleep 2
    current="$(dashboard_fingerprint)"
    [[ "$current" == "$previous" ]] && continue
    if (
      cd "$REPO_ROOT"
      PYTHONPATH="$REPO_ROOT" python3 -m g2_sort.validation.build_motion_dashboard \
        --log-root "$REPO_ROOT/logs" \
        --output "$dashboard"
    ); then
      echo "[$(date '+%H:%M:%S')] Dashboard rebuilt; browser will reload."
      previous="$current"
    else
      echo "[$(date '+%H:%M:%S')] Dashboard rebuild failed; keeping last good page." >&2
    fi
  done
}

if [[ "$watch" == true ]]; then
  watch_dashboard &
  watch_pid="$!"
  trap 'kill "$watch_pid" 2>/dev/null || true' EXIT INT TERM
  echo "Hot reload: watching telemetry JSONL and dashboard generator every 2 seconds."
fi
echo "Dashboard: http://$bind:$port/"
python3 -m http.server "$port" --bind "$bind" --directory "$dashboard"

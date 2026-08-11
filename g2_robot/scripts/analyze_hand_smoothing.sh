#!/usr/bin/env bash
# 用途：从powered telemetry离线比较手部5Hz平滑候选；绝不连接GDK。
# 输入：一个或多个包含action_chunk和dispatch_summary的JSONL文件。
# 输出：独立jerk正则、因果单步限幅和V1状态跟踪器的步进、跟踪误差与时序偏移。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

if (( $# == 0 )); then
  echo "usage: $0 LOG.jsonl [LOG2.jsonl ...]" >&2
  exit 2
fi

cd "$REPO_ROOT"
python3 -m g2_sort.validation.evaluate_hand_smoothing "$@"

#!/usr/bin/env bash
# 用途：从powered telemetry离线比较双臂chunk内部平滑候选；绝不连接GDK。
# 输入：一个或多个包含action_chunk和dispatch_summary的JSONL文件。
# 输出：原始、FIR5和固定端点jerk正则化候选的轨迹指标表。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

if (( $# == 0 )); then
  echo "usage: $0 LOG.jsonl [LOG2.jsonl ...]" >&2
  exit 2
fi

cd "$REPO_ROOT"
python3 -m g2_sort.validation.evaluate_chunk_smoothing "$@"

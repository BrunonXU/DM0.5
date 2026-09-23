#!/usr/bin/env bash
# Read-only feedback timing probe. It initializes GDK but never sends motion.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

assert_no_controller_process
load_g2_environment
section "34维硬件反馈只读速率Probe"
echo "本命令不会调用Servo或move_ee_pos；机器人必须保持静止。"
echo "依次测试5/10/20Hz，每档300次，约需105秒。"
read -r -p "确认机器人静止且当前没有控制进程，输入 ok 继续：" answer
[[ "${answer,,}" == "ok" ]] || { echo "STOP: 已取消" >&2; exit 2; }

cd "$REPO_ROOT"
python3.12 -m g2_sort.operations.diagnostics.joint_feedback_rate_probe \
  --output logs/diagnostics/joint_feedback_probe_latest.json \
  --samples 300

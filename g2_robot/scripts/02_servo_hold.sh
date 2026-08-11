#!/usr/bin/env bash
# 用途：只读预览当前位置；危险current-hold真实执行当前锁定。
# 输入：原样转发给servo_hold模块的预览参数。
# 输出：终端验收信息；GDK原生日志统一进入logs/gdk_native。
# 方法：只读取状态；--execute会被Python安全锁直接拒绝。
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

assert_no_controller_process
load_g2_environment

section "当前位置Servo hold"
echo "当前只允许预览；--execute已软件锁定，零手指反馈不能作为安全hold目标。"
python3.12 -m g2_sort.operations.diagnostics.servo_hold "$@"

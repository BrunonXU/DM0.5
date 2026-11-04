#!/usr/bin/env bash
# Passive evidence only. The existing HIL process remains the sole motion owner.
set -eu
tool_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
capture_dir="/home/user/Bruno/DAgger-runtime/right-home-audit/$(date -u +%Y%m%dT%H%M%SZ)-$$"
printf '只读取证：按 Ctrl+C 保存结束，最长10分钟；128MiB循环保留最近报文，不发送控制命令。\n'
printf '须先看到“采证有效”再操作；sudo打开监听后降回当前用户保存文件。\n'
sudo /usr/bin/python3 "$tool_dir/69_capture_arm_udp_readonly.py" --seconds 600 --out "$capture_dir"
printf '采证完成：%s\n' "$capture_dir"

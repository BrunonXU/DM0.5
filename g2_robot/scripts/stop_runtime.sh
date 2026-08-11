#!/usr/bin/env bash
# 用途：软件请求当前正式G2 VLA客户端优雅停止。
# 输入：可选G2_RUNTIME_PID_FILE环境变量。
# 输出：SIGINT停止结果；不会强杀未知进程或使用kill -9。
# 方法：核对PID和cmdline后发送SIGINT，并有界等待5秒。
set -Eeuo pipefail

PID_FILE="${G2_RUNTIME_PID_FILE:-/tmp/g2_dm05_runtime.pid}"
if [[ ! -f "$PID_FILE" ]]; then
  echo "没有运行时PID文件：$PID_FILE"
  exit 0
fi

PID="$(tr -d '[:space:]' < "$PID_FILE")"
if ! [[ "$PID" =~ ^[1-9][0-9]*$ ]] || [[ ! -d "/proc/$PID" ]]; then
  echo "清理失效PID文件：$PID_FILE"
  rm -f -- "$PID_FILE"
  exit 0
fi

CMDLINE="$(tr '\0' ' ' < "/proc/$PID/cmdline")"
if [[ "$CMDLINE" != *"g2_sort.deployment.g2_sdk_client"* ]]; then
  echo "STOP: PID $PID 不是G2正式客户端：$CMDLINE" >&2
  exit 1
fi

echo "向G2客户端PID $PID发送SIGINT"
kill -INT "$PID"
for _ in $(seq 1 50); do
  if [[ ! -d "/proc/$PID" ]]; then
    echo "PASS: G2客户端已优雅退出"
    exit 0
  fi
  sleep 0.1
done
echo "STOP: 进程未在5秒内退出；请使用物理急停并人工检查，脚本不会kill -9" >&2
exit 1

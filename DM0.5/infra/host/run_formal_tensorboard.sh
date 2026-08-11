#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/home/xza_100850113/projects/DM0.5"
PYTHON_PACKAGES="${PROJECT_ROOT}/server/monitoring/python_packages"
MONITOR_ROOT="${PROJECT_ROOT}/server/monitoring"

declare -a exporter_pids=()

start_exporter() {
  local input_log="$1"
  local run_name="$2"
  local mode="$3"
  local event_dir="${MONITOR_ROOT}/${run_name}"

  mkdir -p "${event_dir}"
  if [[ "${mode}" == "once" ]]; then
    PYTHONPATH="${PYTHON_PACKAGES}" python3 \
      "${PROJECT_ROOT}/infra/host/export_train_log_tensorboard.py" \
      --input "${input_log}" \
      --log-dir "${event_dir}" \
      --once \
      >>"${MONITOR_ROOT}/exporter.log" 2>&1 &
  else
    PYTHONPATH="${PYTHON_PACKAGES}" python3 \
      "${PROJECT_ROOT}/infra/host/export_train_log_tensorboard.py" \
      --input "${input_log}" \
      --log-dir "${event_dir}" \
      --poll-seconds 15 \
      >>"${MONITOR_ROOT}/exporter.log" 2>&1 &
  fi
  exporter_pids+=("$!")
}

start_exporter \
  "${PROJECT_ROOT}/server/runs/formal_50k/train.log" \
  "formal_50k" \
  "once"
start_exporter \
  "${PROJECT_ROOT}/server/runs/formal_50k_to_100k/train.log" \
  "extension_50k_to_100k" \
  "follow"

cleanup() {
  local exporter_pid
  for exporter_pid in "${exporter_pids[@]}"; do
    kill "${exporter_pid}" 2>/dev/null || true
  done
  for exporter_pid in "${exporter_pids[@]}"; do
    wait "${exporter_pid}" 2>/dev/null || true
  done
}
trap cleanup EXIT

# Localhost-only by design. Access through VS Code port forwarding or an SSH
# tunnel; do not expose training telemetry directly on the LAN.
PYTHONPATH="${PYTHON_PACKAGES}" python3 -m tensorboard.main \
  --logdir "${MONITOR_ROOT}" \
  --host 127.0.0.1 \
  --port 6006 \
  2>&1 | tee "${MONITOR_ROOT}/tensorboard.log"

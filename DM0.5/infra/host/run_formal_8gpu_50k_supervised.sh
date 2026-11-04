#!/usr/bin/env bash
set -u

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNNER="${PROJECT_ROOT}/infra/host/run_formal_8gpu_50k.sh"
RUN_ROOT="${PROJECT_ROOT}/server/runs/formal_50k"

mkdir -p "${RUN_ROOT}"

# Keep the formal job alive inside the named tmux session. If Docker or training
# exits non-zero, the official trainer will discover the newest complete
# checkpoint on the next invocation and resume automatically.
while true; do
  "${RUNNER}"
  rc=$?
  printf 'runner_exit=%s rc=%s\n' "$(date --iso-8601=seconds)" "${rc}" \
    >>"${RUN_ROOT}/supervisor.log"
  if [[ "${rc}" -eq 0 ]]; then
    exit 0
  fi
  sleep 30
done

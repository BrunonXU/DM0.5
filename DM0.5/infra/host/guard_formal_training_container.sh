#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "usage: $0 CONTAINER_ID" >&2
  exit 2
fi

PROJECT_ROOT="/home/xza_100850113/projects/DM0.5"
CONTAINER_ID="$1"
RUN_ROOT="${PROJECT_ROOT}/server/runs/formal_50k"

# docker wait prints the container's exit code. The original training container
# uses --rm, so waiting now is the reliable way to retain that code before the
# daemon removes the container.
container_exit="$(
  sg docker -c "docker wait ${CONTAINER_ID}" 2>>"${RUN_ROOT}/guardian.log"
)" || container_exit="125"

printf 'guardian_event=%s container=%s exit=%s\n' \
  "$(date --iso-8601=seconds)" "${CONTAINER_ID}" "${container_exit}" \
  >>"${RUN_ROOT}/guardian.log"

if [[ "${container_exit}" == "0" ]]; then
  exit 0
fi

# A non-zero exit is a failure, not normal 50K completion. The official trainer
# will discover the newest complete checkpoint and resume from it.
exec "${PROJECT_ROOT}/infra/host/run_formal_8gpu_50k_supervised.sh"

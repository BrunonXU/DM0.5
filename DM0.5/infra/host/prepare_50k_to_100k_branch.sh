#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="docker.1ms.run/dexmal/opendm@sha256:REDACTED_HOST_FINGERPRINT"
SOURCE="${PROJECT_ROOT}/server/checkpoints/dm05_g2_8gpu_50k/checkpoint-50000"
OUTPUT="${PROJECT_ROOT}/server/checkpoints/dm05_g2_8gpu_50k_to_100k"
TARGET="${OUTPUT}/checkpoint-50000"
MARKER="${OUTPUT}/.continuation_state_prepared"

if [[ -f "${MARKER}" ]]; then
  exit 0
fi
if [[ -e "${TARGET}" ]]; then
  echo "Refusing ambiguous partial continuation checkpoint: ${TARGET}" >&2
  exit 1
fi
cp -a "${SOURCE}" "${TARGET}"

sg docker -c "docker run --rm \
  -v ${PROJECT_ROOT}:/workspace \
  -w /workspace \
  ${IMAGE} \
  bash -lc 'source /opt/conda/etc/profile.d/conda.sh && \
    conda activate opendm && \
    python - <<\"PY\"
from pathlib import Path
import torch

root = Path(\"server/checkpoints/dm05_g2_8gpu_50k_to_100k/checkpoint-50000\")
optimizer_path = root / \"optimizer.pt\"
scheduler_path = root / \"scheduler.pt\"
optimizer = torch.load(optimizer_path, map_location=\"cpu\", weights_only=False)
for group in optimizer[\"param_groups\"]:
    group[\"lr\"] = 5e-5
    group[\"initial_lr\"] = 5e-5
torch.save(optimizer, optimizer_path)
scheduler = torch.load(scheduler_path, map_location=\"cpu\", weights_only=False)
scheduler[\"base_lrs\"] = [5e-5 for _ in scheduler[\"base_lrs\"]]
scheduler[\"_last_lr\"] = [5e-5 for _ in scheduler[\"_last_lr\"]]
torch.save(scheduler, scheduler_path)
PY'"

printf 'prepared=%s\nsource=%s\ntarget=%s\nlr=5e-5\n' \
  "$(date --iso-8601=seconds)" "${SOURCE}" "${TARGET}" >"${MARKER}"

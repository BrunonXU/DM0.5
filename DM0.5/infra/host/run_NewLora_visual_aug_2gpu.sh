#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NVME_MOUNT="/mnt/dm05_nvme"
NVME_UUID="bd6a10fd-b6fe-428e-ade2-7f3130ea902a"
NVME_DM05_ROOT="${NVME_MOUNT}/dm05"
IMAGE="opendm:g2-34d-fa2"
CHECKPOINT_REL="checkpoints/NewLora_g2_armrel_handabs_officialaug_4gpu_75k_v1"
EVAL_REL="evaluations/NewLora_validation_v1"
MODEL_REL="model_cache/Dexmal/DM05"
NORM_REL="processed/full/NewLora_officialaug_norm_stats"
MANIFEST="/workspace/server/${EVAL_REL}/manifests/actions_80ep_4frames.json"

if ! mountpoint -q "${NVME_MOUNT}"; then
  echo "DM0.5 data disk is not mounted at ${NVME_MOUNT}." >&2
  exit 2
fi
mounted_uuid="$(findmnt -rn -T "${NVME_MOUNT}" -o UUID)"
if [[ "${mounted_uuid}" != "${NVME_UUID}" ]]; then
  echo "Wrong filesystem at ${NVME_MOUNT}: expected UUID=${NVME_UUID}, got ${mounted_uuid:-unknown}." >&2
  exit 2
fi
if [[ ! -d "${NVME_DM05_ROOT}/${CHECKPOINT_REL}" ]]; then
  echo "NewLora checkpoints are missing: ${NVME_DM05_ROOT}/${CHECKPOINT_REL}" >&2
  exit 2
fi

launch_worker() {
  local physical_gpu="$1"
  local step="$2"
  local container="NewLora-visual-aug-${step}"
  if docker ps -a --format '{{.Names}}' | grep -Fxq "${container}"; then
    echo "Refusing to replace existing container: ${container}" >&2
    return 2
  fi
  docker run -d \
    --name "${container}" \
    --restart on-failure:3 \
    --gpus "device=${physical_gpu}" \
    --shm-size=16g \
    -e PYTHONUNBUFFERED=1 \
    -e PYTHONPATH=/workspace/opendm:/workspace \
    -e G2_SORT_DATA_ROOT=/workspace/server/processed/full \
    -v "${PROJECT_ROOT}:/workspace:ro" \
    -v "${NVME_DM05_ROOT}:/workspace/server:rw" \
    -w /workspace \
    --entrypoint bash \
    "${IMAGE}" -lc "
      set -Eeuo pipefail
      source /opt/conda/etc/profile.d/conda.sh
      conda activate opendm
      mkdir -p /workspace/server/${EVAL_REL}/results/visual_aug /workspace/server/${EVAL_REL}/logs
      python g2_sort/validation/evaluate_NewLora_visual_aug.py \
        --model /workspace/server/${CHECKPOINT_REL}/checkpoint-${step} \
        --processor /workspace/server/${MODEL_REL} \
        --norm-stats-root /workspace/server/${NORM_REL} \
        --manifest '${MANIFEST}' \
        --batch-size 2 \
        --seed-repeats 3 \
        --seed 20260822 \
        --diffusion-steps 10 \
        --device cuda:0 \
        --output /workspace/server/${EVAL_REL}/results/visual_aug/checkpoint-${step}.json \
        2>&1 | tee -a /workspace/server/${EVAL_REL}/logs/visual-aug-${step}.log
    "
  printf 'started %s on physical GPU %s\n' "${container}" "${physical_gpu}"
}

launch_worker 0 70000
launch_worker 2 75000

printf 'Visual E6-A started: 70K/GPU0 and 75K/GPU2. GPU4 inference remains online.\n'

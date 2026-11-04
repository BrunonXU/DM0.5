#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NVME_MOUNT="/mnt/dm05_nvme"
NVME_UUID="bd6a10fd-b6fe-428e-ade2-7f3130ea902a"
NVME_DM05_ROOT="${NVME_MOUNT}/dm05"
IMAGE="opendm:g2-34d-fa2"
EVAL_REL="evaluations/NewLora_validation_v1"
CHECKPOINT_REL="checkpoints/NewLora_g2_armrel_handabs_officialaug_4gpu_75k_v1"
NORM_REL="processed/full/NewLora_officialaug_norm_stats"
MODEL_REL="model_cache/Dexmal/DM05"
FM_MANIFEST="/workspace/server/${EVAL_REL}/manifests/fm_80ep_16frames.json"
ACTION_MANIFEST="/workspace/server/${EVAL_REL}/manifests/actions_80ep_4frames.json"

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

declare -A GPU_STEPS
GPU_STEPS[4]="5000 25000 45000 65000"
GPU_STEPS[5]="10000 30000 50000 70000"
GPU_STEPS[6]="15000 35000 55000 75000"
GPU_STEPS[7]="20000 40000 60000"

mkdir -p "${PROJECT_ROOT}/server"

for gpu in 4 5 6 7; do
  container="NewLora-validation-gpu${gpu}-v1"
  steps="${GPU_STEPS[$gpu]}"
  docker rm -f "${container}" >/dev/null 2>&1 || true
  docker run -d \
    --name "${container}" \
    --restart on-failure:3 \
    --gpus "device=${gpu}" \
    --ipc host \
    -e PYTHONUNBUFFERED=1 \
    -e PYTHONPATH=/workspace/opendm:/workspace \
    -e G2_SORT_DATA_ROOT=/workspace/server/processed/full \
    -v "${PROJECT_ROOT}:/workspace:ro" \
    -v "${NVME_DM05_ROOT}:/workspace/server:rw" \
    --entrypoint bash \
    "${IMAGE}" -lc "
      set -euo pipefail
      source /opt/conda/etc/profile.d/conda.sh
      conda activate opendm
      cd /workspace
      log=/workspace/server/${EVAL_REL}/logs/gpu${gpu}.log
      exec > >(tee -a \"\${log}\") 2>&1
      printf 'worker_start=%s physical_gpu=${gpu} steps=%s\\n' \"\$(date --iso-8601=seconds)\" '${steps}'
      for step in ${steps}; do
        checkpoint=/workspace/server/${CHECKPOINT_REL}/checkpoint-\${step}
        fm=/workspace/server/${EVAL_REL}/results/fm/checkpoint-\${step}.json
        actions=/workspace/server/${EVAL_REL}/results/actions/checkpoint-\${step}.json
        if [[ ! -s \"\${fm}\" ]]; then
          printf 'fm_start=%s checkpoint=%s\\n' \"\$(date --iso-8601=seconds)\" \"\${step}\"
          python g2_sort/validation/evaluate_NewLora_fm.py \
            --model \"\${checkpoint}\" \
            --processor /workspace/server/${MODEL_REL} \
            --norm-stats-root /workspace/server/${NORM_REL} \
            --manifest '${FM_MANIFEST}' \
            --batch-size 4 \
            --seed-repeats 3 \
            --seed 20260817 \
            --device cuda:0 \
            --output \"\${fm}\"
          printf 'fm_complete=%s checkpoint=%s\\n' \"\$(date --iso-8601=seconds)\" \"\${step}\"
        fi
        if [[ ! -s \"\${actions}\" ]]; then
          printf 'actions_start=%s checkpoint=%s\\n' \"\$(date --iso-8601=seconds)\" \"\${step}\"
          python g2_sort/validation/evaluate_NewLora_actions.py \
            --model \"\${checkpoint}\" \
            --processor /workspace/server/${MODEL_REL} \
            --norm-stats-root /workspace/server/${NORM_REL} \
            --manifest '${ACTION_MANIFEST}' \
            --batch-size 2 \
            --seed-repeats 2 \
            --seed 20260818 \
            --diffusion-steps 10 \
            --device cuda:0 \
            --output \"\${actions}\"
          printf 'actions_complete=%s checkpoint=%s\\n' \"\$(date --iso-8601=seconds)\" \"\${step}\"
        fi
      done
      printf 'worker_complete=%s physical_gpu=${gpu}\\n' \"\$(date --iso-8601=seconds)\"
    "
  printf '%s started on physical GPU %s\n' "${container}" "${gpu}"
done

tmux kill-session -t NewLora-validation-v1 >/dev/null 2>&1 || true
tmux new-session -d -s NewLora-validation-v1 \
  "watch -n 10 'sg docker -c \"docker ps -a --filter name=NewLora-validation-gpu --format \\\"table {{.Names}}\\\\t{{.Status}}\\\"\"; nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader | tail -n 4'"

printf 'NewLora validation started on GPUs 4,5,6,7.\n'

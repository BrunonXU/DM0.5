#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
G2_ROBOT_ROOT="$(dirname "$PROJECT_ROOT")/g2_robot"
NVME_DM05_ROOT="/mnt/dm05_nvme/dm05"
GPU_INDEX="${NEWLORA_RTC_EVAL_GPU:-5}"
WEIGHT="${NEWLORA_RTC_MAX_GUIDANCE_WEIGHT:-0.5}"
GUIDANCE_MODE="${NEWLORA_RTC_GUIDANCE_MODE:-approx_identity}"
PAIRS="${NEWLORA_RTC_EVAL_PAIRS:-32}"
DIFFUSION_STEPS="${NEWLORA_RTC_DIFFUSION_STEPS:-10}"
DELAY_STEPS="${NEWLORA_RTC_DELAY_STEPS:-20}"
EXECUTION_HORIZON="${NEWLORA_RTC_EXECUTION_HORIZON:-25}"
COUPLE_ADJACENT_NOISE="${NEWLORA_RTC_COUPLE_ADJACENT_NOISE:-false}"
TAG="${WEIGHT//./p}_d${DELAY_STEPS}_s${EXECUTION_HORIZON}"
if ! [[ "$DIFFUSION_STEPS" =~ ^[1-9][0-9]*$ ]]; then
  echo "NEWLORA_RTC_DIFFUSION_STEPS must be a positive integer" >&2
  exit 2
fi
if [[ "$DIFFUSION_STEPS" != "10" ]]; then
  TAG="${TAG}_steps${DIFFUSION_STEPS}"
fi
if ! [[ "$DELAY_STEPS" =~ ^[0-9]+$ && "$EXECUTION_HORIZON" =~ ^[0-9]+$ ]]; then
  echo "NEWLORA_RTC_DELAY_STEPS and NEWLORA_RTC_EXECUTION_HORIZON must be integers" >&2
  exit 2
fi
if (( DELAY_STEPS > EXECUTION_HORIZON || EXECUTION_HORIZON > 50 - DELAY_STEPS )); then
  echo "RTC requires d <= s <= 50-d; got d=$DELAY_STEPS s=$EXECUTION_HORIZON" >&2
  exit 2
fi
EXTRA_ARGS=()
if [[ "$COUPLE_ADJACENT_NOISE" == "true" ]]; then
  TAG="${TAG}_coupled"
  EXTRA_ARGS+=(--couple-adjacent-noise)
elif [[ "$COUPLE_ADJACENT_NOISE" != "false" ]]; then
  echo "NEWLORA_RTC_COUPLE_ADJACENT_NOISE must be true or false" >&2
  exit 2
fi

docker run --rm \
  --name "NewLora-rtc-offline-${TAG}" \
  --gpus "device=${GPU_INDEX}" --ipc host \
  -e PYTHONUNBUFFERED=1 \
  -e PYTHONPATH=/workspace/opendm:/workspace \
  -e G2_SORT_DATA_ROOT=/workspace/server/processed/full \
  -v "${PROJECT_ROOT}:/workspace:ro" \
  -v "${G2_ROBOT_ROOT}:/g2_robot:ro" \
  -v "${NVME_DM05_ROOT}:/workspace/server:rw" \
  -w /workspace \
  --entrypoint /opt/conda/envs/opendm/bin/python \
  opendm:g2-34d-fa2 \
    g2_sort/validation/evaluate_NewLora_rtc.py \
    --model /workspace/server/checkpoints/NewLora_g2_armrel_handabs_officialaug_4gpu_75k_v1/checkpoint-75000 \
    --processor /workspace/server/model_cache/Dexmal/DM05 \
    --norm-stats-root /workspace/server/processed/full/NewLora_officialaug_norm_stats \
    --manifest /workspace/server/evaluations/NewLora_validation_v1/manifests/actions_80ep_4frames.json \
    --device cuda:0 --seed 20260818 --diffusion-steps "${DIFFUSION_STEPS}" \
    --delay-steps "${DELAY_STEPS}" --execution-horizon "${EXECUTION_HORIZON}" \
    --guidance-mode "${GUIDANCE_MODE}" \
    --max-guidance-weight "${WEIGHT}" --max-pairs "${PAIRS}" \
    "${EXTRA_ARGS[@]}" \
    --g2-client-dir /g2_robot/g2_sort/deployment/g2_client \
    --output "/workspace/server/evaluations/NewLora_RTC_v1/adjacent_weight_${TAG}_${PAIRS}pairs.json"

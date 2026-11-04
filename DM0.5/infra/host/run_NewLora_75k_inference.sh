#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NVME_MOUNT="/mnt/dm05_nvme"
NVME_UUID="bd6a10fd-b6fe-428e-ade2-7f3130ea902a"
NVME_DM05_ROOT="${NVME_MOUNT}/dm05"
IMAGE="opendm:g2-34d-fa2"
CHECKPOINT_REL="checkpoints/NewLora_g2_armrel_handabs_officialaug_4gpu_75k_v1/checkpoint-75000"
RUN_REL="runs/NewLora/deployment_75k"
CONTAINER_NAME="NewLora-dm05-http-75k"
GPU_INDEX="${NEWLORA_INFERENCE_GPU:-1}"
PORT="${NEWLORA_INFERENCE_PORT:-7892}"
DIFFUSION_STEPS="${NEWLORA_INFERENCE_DIFFUSION_STEPS:-5}"
COMPILE_ACTION_EXPERT="${NEWLORA_INFERENCE_COMPILE_ACTION_EXPERT:-false}"

if ! [[ "${DIFFUSION_STEPS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "NEWLORA_INFERENCE_DIFFUSION_STEPS must be a positive integer." >&2
  exit 2
fi
if [[ "${COMPILE_ACTION_EXPERT}" != "true" && "${COMPILE_ACTION_EXPERT}" != "false" ]]; then
  echo "NEWLORA_INFERENCE_COMPILE_ACTION_EXPERT must be true or false." >&2
  exit 2
fi
COMPILE_ARGS=()
COMPILE_ENV_ARGS=()
if [[ "${COMPILE_ACTION_EXPERT}" == "true" ]]; then
  COMPILE_ARGS+=(--inference-config.compile-action-expert)
  # Emit only actual guard failures/recompiles. Stable requests add no log noise.
  COMPILE_ENV_ARGS+=(-e TORCH_LOGS=recompiles)
fi

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
  echo "Checkpoint is missing: ${NVME_DM05_ROOT}/${CHECKPOINT_REL}" >&2
  exit 2
fi

if docker ps -a --format '{{.Names}}' | grep -Fxq "${CONTAINER_NAME}"; then
  echo "Refusing to replace existing container: ${CONTAINER_NAME}" >&2
  echo "Inspect or remove that exact container explicitly before relaunching." >&2
  exit 2
fi

docker run -d \
  --name "${CONTAINER_NAME}" \
  --restart unless-stopped \
  --gpus "device=${GPU_INDEX}" \
  --shm-size=16g \
  -p "${PORT}:${PORT}" \
  -e PYTHONUNBUFFERED=1 \
  -e PYTHONPATH=/workspace/opendm:/workspace \
  -e G2_SORT_DATA_ROOT=/workspace/server/processed/full \
  "${COMPILE_ENV_ARGS[@]}" \
  -v "${PROJECT_ROOT}:/workspace:ro" \
  -v "${NVME_DM05_ROOT}:/workspace/server:rw" \
  -w /workspace \
  --entrypoint bash \
  "${IMAGE}" -lc "
    set -Eeuo pipefail
    source /opt/conda/etc/profile.d/conda.sh
    conda activate opendm
    mkdir -p /workspace/server/${RUN_REL}
    printf 'server_start=%s checkpoint=%s gpu=%s port=%s action=arm_relative_14+hand_absolute_20 diffusion_steps=%s compile_action_expert=%s output=50x34\\n' \
      \"\$(date --iso-8601=seconds)\" '${CHECKPOINT_REL}' '${GPU_INDEX}' '${PORT}' \
      '${DIFFUSION_STEPS}' '${COMPILE_ACTION_EXPERT}' \
      >>/workspace/server/${RUN_REL}/launch_history.log
    exec python -m g2_sort.training.NewLora_dm05_lora \
      --task inference \
      --model-config.model-name-or-path /workspace/server/${CHECKPOINT_REL} \
      --model-config.llm-attn-implementation eager \
      --model-config.vision-attn-implementation flash_attention_2 \
      --model-config.action-attn-implementation sdpa \
      --inference-config.port ${PORT} \
      --inference-config.diffusion-steps ${DIFFUSION_STEPS} \
      --inference-config.output-action-dim 34 \
      ${COMPILE_ARGS[*]} \
      2>&1 | tee -a /workspace/server/${RUN_REL}/server.log
  "

printf 'NewLora 75K inference starting: container=%s gpu=%s port=%s diffusion_steps=%s compile_action_expert=%s\n' \
  "${CONTAINER_NAME}" "${GPU_INDEX}" "${PORT}" "${DIFFUSION_STEPS}" "${COMPILE_ACTION_EXPERT}"
printf 'Follow logs: docker logs -f --tail 200 %s\n' "${CONTAINER_NAME}"

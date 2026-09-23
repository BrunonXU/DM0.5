#!/usr/bin/env bash
set -Eeuo pipefail

# Separate RTC service: it never replaces the validated non-RTC port/container.
PROJECT_ROOT="/home/xza_100850113/projects/DM0.5"
NVME_MOUNT="/mnt/dm05_nvme"
NVME_UUID="bd6a10fd-b6fe-428e-ade2-7f3130ea902a"
NVME_DM05_ROOT="${NVME_MOUNT}/dm05"
IMAGE="${NEWLORA_RTC_WS_IMAGE:-opendm:g2-34d-fa2-ws}"
CHECKPOINT_REL="checkpoints/NewLora_g2_armrel_handabs_officialaug_4gpu_75k_v1/checkpoint-75000"
RUN_REL="runs/NewLora/rtc_deployment_75k"
CONTAINER_NAME="${NEWLORA_RTC_CONTAINER:-NewLora-dm05-ws-75k-rtc}"
GPU_INDEX="${NEWLORA_RTC_INFERENCE_GPU:-3}"
PORT="${NEWLORA_RTC_INFERENCE_PORT:-7893}"
MAX_GUIDANCE_WEIGHT="${NEWLORA_RTC_MAX_GUIDANCE_WEIGHT:-0.5}"
GUIDANCE_MODE="${NEWLORA_RTC_GUIDANCE_MODE:-approx_identity}"
DIFFUSION_STEPS="${NEWLORA_RTC_INFERENCE_DIFFUSION_STEPS:-5}"
COMPILE_ACTION_EXPERT="${NEWLORA_RTC_INFERENCE_COMPILE_ACTION_EXPERT:-true}"

if ! [[ "${DIFFUSION_STEPS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "NEWLORA_RTC_INFERENCE_DIFFUSION_STEPS must be a positive integer." >&2
  exit 2
fi
if [[ "${COMPILE_ACTION_EXPERT}" != "true" && "${COMPILE_ACTION_EXPERT}" != "false" ]]; then
  echo "NEWLORA_RTC_INFERENCE_COMPILE_ACTION_EXPERT must be true or false." >&2
  exit 2
fi
COMPILE_ARGS=()
COMPILE_ENV_ARGS=()
if [[ "${COMPILE_ACTION_EXPERT}" == "true" ]]; then
  COMPILE_ARGS+=(--inference-config.compile-action-expert)
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
  "${IMAGE}" -lc "source /opt/conda/etc/profile.d/conda.sh && conda activate opendm && mkdir -p /workspace/server/${RUN_REL} && exec python -m g2_sort.training.NewLora_dm05_lora --task inference --model-config.model-name-or-path /workspace/server/${CHECKPOINT_REL} --model-config.llm-attn-implementation eager --model-config.vision-attn-implementation flash_attention_2 --model-config.action-attn-implementation sdpa --inference-config.port ${PORT} --inference-config.transport websocket-binary --inference-config.diffusion-steps ${DIFFUSION_STEPS} --inference-config.output-action-dim 34 --inference-config.rtc-enabled --inference-config.rtc-guidance-mode ${GUIDANCE_MODE} --inference-config.rtc-max-guidance-weight ${MAX_GUIDANCE_WEIGHT} ${COMPILE_ARGS[*]} 2>&1 | tee -a /workspace/server/${RUN_REL}/server.log"

printf 'RTC inference starting: container=%s gpu=%s port=%s mode=%s max_weight=%s diffusion_steps=%s compile_action_expert=%s\n' \
  "${CONTAINER_NAME}" "${GPU_INDEX}" "${PORT}" "${GUIDANCE_MODE}" "${MAX_GUIDANCE_WEIGHT}" \
  "${DIFFUSION_STEPS}" "${COMPILE_ACTION_EXPERT}"
printf 'This service is for offline/shadow validation first; do not use powered mode yet.\n'
printf 'Transport=native JPEG/PNG + persistent binary WebSocket; output=50x34 float32.\n'

#!/usr/bin/env bash
# Start an isolated HTTP control for the binary WebSocket transport experiment.
# It matches the 7896 candidate's checkpoint, GPU, five diffusion steps, and compile mode.
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NVME_MOUNT="/mnt/dm05_nvme"
NVME_UUID="bd6a10fd-b6fe-428e-ade2-7f3130ea902a"
NVME_DM05_ROOT="${NVME_MOUNT}/dm05"
IMAGE="${NEWLORA_HTTP_CANDIDATE_IMAGE:-opendm:g2-34d-fa2-ws}"
CHECKPOINT_REL="checkpoints/NewLora_g2_armrel_handabs_officialaug_4gpu_75k_v1/checkpoint-75000"
RUN_REL="runs/NewLora/http_transport_candidate_75k"
CONTAINER_NAME="${NEWLORA_HTTP_CANDIDATE_CONTAINER:-NewLora-dm05-http-75k-transport}"
GPU_INDEX="${NEWLORA_HTTP_CANDIDATE_GPU:-1}"
PORT="${NEWLORA_HTTP_CANDIDATE_PORT:-7897}"

if ! mountpoint -q "$NVME_MOUNT"; then
  echo "DM0.5 data disk is not mounted at $NVME_MOUNT." >&2
  exit 2
fi
mounted_uuid="$(findmnt -rn -T "$NVME_MOUNT" -o UUID)"
if [[ "$mounted_uuid" != "$NVME_UUID" ]]; then
  echo "Wrong filesystem at $NVME_MOUNT: expected $NVME_UUID, got ${mounted_uuid:-unknown}." >&2
  exit 2
fi
if [[ ! -d "$NVME_DM05_ROOT/$CHECKPOINT_REL" ]]; then
  echo "Checkpoint is missing: $NVME_DM05_ROOT/$CHECKPOINT_REL" >&2
  exit 2
fi
if sg docker -c "docker ps -a --format '{{.Names}}'" | grep -Fxq "$CONTAINER_NAME"; then
  echo "Refusing to replace existing container: $CONTAINER_NAME" >&2
  exit 2
fi

sg docker -c "docker run -d \
  --name $CONTAINER_NAME \
  --restart no \
  --gpus device=$GPU_INDEX \
  --shm-size=16g \
  -p $PORT:$PORT \
  -e PYTHONUNBUFFERED=1 \
  -e PYTHONPATH=/workspace/opendm:/workspace \
  -e G2_SORT_DATA_ROOT=/workspace/server/processed/full \
  -e TORCH_LOGS=recompiles \
  -v $PROJECT_ROOT:/workspace:ro \
  -v $NVME_DM05_ROOT:/workspace/server:rw \
  -w /workspace \
  --entrypoint bash \
  $IMAGE -lc 'source /opt/conda/etc/profile.d/conda.sh && conda activate opendm && mkdir -p /workspace/server/$RUN_REL && exec python -m g2_sort.training.NewLora_dm05_lora --task inference --model-config.model-name-or-path /workspace/server/$CHECKPOINT_REL --model-config.llm-attn-implementation eager --model-config.vision-attn-implementation flash_attention_2 --model-config.action-attn-implementation sdpa --inference-config.port $PORT --inference-config.transport http-multipart-json --inference-config.diffusion-steps 5 --inference-config.output-action-dim 34 --inference-config.compile-action-expert 2>&1 | tee -a /workspace/server/$RUN_REL/server.log'"

printf 'HTTP transport control starting: container=%s gpu=%s port=%s\n' \
  "$CONTAINER_NAME" "$GPU_INDEX" "$PORT"
printf 'HTTP 7892 and RTC 7893 were not modified.\n'

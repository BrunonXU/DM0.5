#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="opendm:g2-34d-fa2"
NVME_MOUNT="/mnt/dm05_nvme"
NVME_UUID="bd6a10fd-b6fe-428e-ade2-7f3130ea902a"
NVME_DM05_ROOT="${NVME_MOUNT}/dm05"
CONTAINER_NAME="NewLora-g2-armrel-handabs-officialaug-4gpu-75k-v1"
TMUX_NAME="NewLora-officialaug-g2-4gpu-75k-v1"
WANDB_CREDENTIAL_VOLUME="NewLora-wandb-credentials"
RUN_REL="runs/NewLora/g2_armrel_handabs_officialaug_4gpu_75k_v1"
OUTPUT_REL="checkpoints/NewLora_g2_armrel_handabs_officialaug_4gpu_75k_v1"

if ! mountpoint -q "${NVME_MOUNT}"; then
  echo "DM0.5 data disk is not mounted at ${NVME_MOUNT}." >&2
  exit 2
fi
mounted_uuid="$(findmnt -rn -T "${NVME_MOUNT}" -o UUID)"
if [[ "${mounted_uuid}" != "${NVME_UUID}" ]]; then
  echo "Wrong filesystem at ${NVME_MOUNT}: expected UUID=${NVME_UUID}, got ${mounted_uuid:-unknown}." >&2
  exit 2
fi
if [[ ! -d "${NVME_DM05_ROOT}/processed/full" || ! -d "${NVME_DM05_ROOT}/model_cache/Dexmal/DM05" ]]; then
  echo "DM0.5 dataset or base model is missing under ${NVME_DM05_ROOT}." >&2
  exit 2
fi

if sg docker -c "docker ps -a --format '{{.Names}}'" | grep -Fxq "${CONTAINER_NAME}"; then
  echo "Refusing to replace existing container: ${CONTAINER_NAME}" >&2
  exit 2
fi

container_command="set -o pipefail; source /opt/conda/etc/profile.d/conda.sh && conda activate opendm && \
  mkdir -p /workspace/server/${RUN_REL}/wandb && \
  { printf 'container_start=%s\\n' \"\$(date --iso-8601=seconds)\"; \
    printf 'recipe=NewLora arm_relative_14 hand_absolute_20 vendor_official_full_aug_p0.5\\n'; \
    printf 'physical_gpus=4,5,6,7 world_size=4 per_device_batch=4 accumulation=2 global_batch=32 steps=75000\\n'; \
    printf 'output=/workspace/server/${OUTPUT_REL} wandb_project=NewLora_DM05_G2 wandb_run_id=${CONTAINER_NAME}\\n'; \
  } >>/workspace/server/${RUN_REL}/launch_history.log; \
  nvidia-smi --query-gpu=timestamp,index,memory.used,utilization.gpu,power.draw \
    --format=csv,noheader,nounits --loop=5 \
    >>/workspace/server/${RUN_REL}/gpu_metrics.csv & monitor_pid=\$!; \
  trap 'kill \$monitor_pid 2>/dev/null || true' EXIT; \
  bash opendm/script/dm05_launcher.sh \
    --exp /workspace/g2_sort/training/NewLora_dm05_lora.py \
    --nproc_per_node 4 --master_port 29517 -- \
    --task train \
    --model-config.model-name-or-path /workspace/server/model_cache/Dexmal/DM05 \
    --model-config.llm-attn-implementation sdpa \
    --model-config.vision-attn-implementation flash_attention_2 \
    --model-config.action-attn-implementation sdpa \
    --trainer-config.output-dir /workspace/server/${OUTPUT_REL} \
    --trainer-config.num-train-steps 75000 \
    --trainer-config.per-device-train-batch-size 4 \
    --trainer-config.gradient-accumulation-steps 2 \
    --trainer-config.save-steps 5000 \
    --trainer-config.save-total-limit 15 \
    --trainer-config.no-save-only-model \
    --trainer-config.dataloader-num-workers 4 \
    --trainer-config.dataloader-persistent-workers \
    --trainer-config.dataloader-prefetch-factor 2 \
    --trainer-config.wandb-project NewLora_DM05_G2 \
    --data-config.norm-stats-root /workspace/server/processed/full/NewLora_officialaug_norm_stats \
    2>&1 | tee -a /workspace/server/${RUN_REL}/train.log"

sg docker -c "docker create \
  --name ${CONTAINER_NAME} \
  --restart on-failure:20 \
  --user 0:0 \
  --gpus all \
  --shm-size=64g \
  -e CUDA_VISIBLE_DEVICES=4,5,6,7 \
  -e HOME=/root \
  -e G2_SORT_DATA_ROOT=/workspace/server/processed/full \
  -e PYTHONPATH=/workspace/opendm:/workspace \
  -e WANDB_PROJECT=NewLora_DM05_G2 \
  -e WANDB_RUN_ID=${CONTAINER_NAME} \
  -e WANDB_RESUME=allow \
  -e WANDB_DIR=/workspace/server/${RUN_REL}/wandb \
  -v ${WANDB_CREDENTIAL_VOLUME}:/root \
  -v ${PROJECT_ROOT}:/workspace \
  -v ${NVME_DM05_ROOT}:/workspace/server \
  -w /workspace \
  ${IMAGE} bash -lc \"${container_command}\"" >/dev/null

sg docker -c "docker start ${CONTAINER_NAME}" >/dev/null
if tmux has-session -t "${TMUX_NAME}" 2>/dev/null; then
  echo "tmux session already exists: ${TMUX_NAME}" >&2
  exit 2
fi
tmux new-session -d -s "${TMUX_NAME}" -c "${PROJECT_ROOT}" \
  "sg docker -c 'docker logs -f --tail 200 ${CONTAINER_NAME}'"

printf 'NewLora started: container=%s tmux=%s\n' "${CONTAINER_NAME}" "${TMUX_NAME}"

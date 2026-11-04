#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="docker.1ms.run/dexmal/opendm@sha256:REDACTED_HOST_FINGERPRINT"
DATA_ROOT="/workspace/server/processed/full"
OUTPUT="/workspace/server/checkpoints/dm05_g2_8gpu_50k"
RUN_ROOT="${PROJECT_ROOT}/server/runs/formal_50k"

mkdir -p "${RUN_ROOT}"
{
  printf 'run_start=%s\n' "$(date --iso-8601=seconds)"
  printf 'image=%s\n' "${IMAGE}"
  printf 'world_size=8 per_device_batch=4 accumulation=1 global_batch=32 steps=50000\n'
  printf 'attention=sdpa/flash_attention_2/sdpa\n'
} >>"${RUN_ROOT}/launch_history.log"

nvidia-smi \
  --query-gpu=timestamp,index,memory.used,utilization.gpu,power.draw \
  --format=csv,noheader,nounits \
  --loop=5 >>"${RUN_ROOT}/gpu_metrics.csv" &
monitor_pid=$!
cleanup() {
  kill "${monitor_pid}" 2>/dev/null || true
  wait "${monitor_pid}" 2>/dev/null || true
}
trap cleanup EXIT

# Full optimizer/scheduler/RNG state is intentionally retained. This makes every
# 5k checkpoint resumable after a host, Docker or user-session interruption.
sg docker -c "docker run --rm \
  --gpus all \
  --shm-size=64g \
  -e CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 \
  -e G2_SORT_DATA_ROOT=${DATA_ROOT} \
  -e PYTHONPATH=/workspace:/workspace/opendm \
  -v ${PROJECT_ROOT}:/workspace \
  -w /workspace \
  ${IMAGE} \
  bash -lc 'source /opt/conda/etc/profile.d/conda.sh && \
    conda activate opendm && \
    bash opendm/script/dm05_launcher.sh \
      --exp /workspace/g2_sort/training/dm05_lora.py \
      --nproc_per_node 8 \
      -- \
      --task train \
      --model-config.model-name-or-path /workspace/server/model_cache/Dexmal/DM05 \
      --model-config.llm-attn-implementation sdpa \
      --model-config.vision-attn-implementation flash_attention_2 \
      --model-config.action-attn-implementation sdpa \
      --trainer-config.output-dir ${OUTPUT} \
      --trainer-config.num-train-steps 50000 \
      --trainer-config.per-device-train-batch-size 4 \
      --trainer-config.gradient-accumulation-steps 1 \
      --trainer-config.save-steps 5000 \
      --trainer-config.save-total-limit 10 \
      --trainer-config.no-save-only-model \
      --trainer-config.dataloader-num-workers 4 \
      --trainer-config.dataloader-persistent-workers \
      --trainer-config.dataloader-prefetch-factor 2 \
      --data-config.norm-stats-root ${DATA_ROOT}/norm_stats'" \
  2>&1 | tee -a "${RUN_ROOT}/train.log"

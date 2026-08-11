#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/home/xza_100850113/projects/DM0.5"
IMAGE="docker.1ms.run/dexmal/opendm@sha256:60a754dce047fbef40ab7d3a160fd82ccfbf5fa0509d862b6702311499dfbd4d"
DATA_ROOT="/workspace/server/processed/full"
OUTPUT="/workspace/server/checkpoints/benchmark_full_8gpu_gb32_500"
RUN_ROOT="${PROJECT_ROOT}/server/runs/full_8gpu_gb32_benchmark"

mkdir -p "${RUN_ROOT}"
nvidia-smi \
  --query-gpu=timestamp,index,memory.used,utilization.gpu,power.draw \
  --format=csv,noheader,nounits \
  --loop=1 >"${RUN_ROOT}/gpu_metrics.csv" &
monitor_pid=$!
cleanup() {
  kill "${monitor_pid}" 2>/dev/null || true
  wait "${monitor_pid}" 2>/dev/null || true
}
trap cleanup EXIT

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
      --trainer-config.num-train-steps 500 \
      --trainer-config.per-device-train-batch-size 4 \
      --trainer-config.gradient-accumulation-steps 1 \
      --trainer-config.save-steps 250 \
      --trainer-config.save-total-limit 2 \
      --trainer-config.no-save-only-model \
      --trainer-config.dataloader-num-workers 4 \
      --trainer-config.dataloader-persistent-workers \
      --trainer-config.dataloader-prefetch-factor 2 \
      --data-config.norm-stats-root ${DATA_ROOT}/norm_stats'" \
  2>&1 | tee "${RUN_ROOT}/train.log"

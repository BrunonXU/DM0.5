#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/home/xza_100850113/projects/DM0.5"
IMAGE="docker.1ms.run/dexmal/opendm@sha256:60a754dce047fbef40ab7d3a160fd82ccfbf5fa0509d862b6702311499dfbd4d"
DATA_ROOT="/workspace/server/processed/full"
OUTPUT="/workspace/server/checkpoints/dm05_g2_8gpu_50k_to_100k"
RUN_ROOT="${PROJECT_ROOT}/server/runs/formal_50k_to_100k"

"${PROJECT_ROOT}/infra/host/prepare_50k_to_100k_branch.sh"
mkdir -p "${RUN_ROOT}"
latest_checkpoint="$(
  find "${PROJECT_ROOT}/server/checkpoints/dm05_g2_8gpu_50k_to_100k" \
    -maxdepth 1 -type d -name 'checkpoint-*' -print |
    sort -V |
    tail -n 1
)"
resume_step="${latest_checkpoint##*-}"
{
  printf 'run_start=%s\n' "$(date --iso-8601=seconds)"
  printf 'image=%s\n' "${IMAGE}"
  printf 'resume_step=%s target_step=100000 global_batch=32 lr=5e-5 scheduler=constant\n' \
    "${resume_step}"
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
      --optimizer-config.base-lr 5e-5 \
      --optimizer-config.warmup-steps 0 \
      --trainer-config.lr-scheduler-type cosine_with_min_lr \
      --trainer-config.lr-scheduler-kwargs.min-lr-rate 1.0 \
      --trainer-config.output-dir ${OUTPUT} \
      --trainer-config.num-train-steps 100000 \
      --trainer-config.per-device-train-batch-size 4 \
      --trainer-config.gradient-accumulation-steps 1 \
      --trainer-config.save-steps 5000 \
      --trainer-config.save-total-limit 11 \
      --trainer-config.no-save-only-model \
      --trainer-config.dataloader-num-workers 4 \
      --trainer-config.dataloader-persistent-workers \
      --trainer-config.dataloader-prefetch-factor 2 \
      --data-config.norm-stats-root ${DATA_ROOT}/norm_stats'" \
  2>&1 | tee -a "${RUN_ROOT}/train.log"

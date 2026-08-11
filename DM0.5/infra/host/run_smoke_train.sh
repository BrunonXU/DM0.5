#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/home/xza_100850113/projects/DM0.5"
IMAGE="docker.1ms.run/dexmal/opendm@sha256:60a754dce047fbef40ab7d3a160fd82ccfbf5fa0509d862b6702311499dfbd4d"
MODEL="/workspace/server/model_cache/Dexmal/DM05"
DATA_ROOT="/workspace/server/processed/smoke_1ep"
OUTPUT="/workspace/server/checkpoints/smoke_1ep/run_20step"
NUM_TRAIN_STEPS="${NUM_TRAIN_STEPS:-20}"
SAVE_STEPS="${SAVE_STEPS:-5}"

sg docker -c "docker run --rm \
  --gpus all \
  --shm-size=16g \
  -e CUDA_VISIBLE_DEVICES=0 \
  -e G2_SORT_DATA_ROOT=${DATA_ROOT} \
  -e PYTHONPATH=/workspace:/workspace/opendm \
  -v ${PROJECT_ROOT}:/workspace \
  -w /workspace \
  ${IMAGE} \
  bash -lc 'source /opt/conda/etc/profile.d/conda.sh && \
    conda activate opendm && \
    bash opendm/script/dm05_launcher.sh \
      --exp /workspace/g2_sort/training/dm05_lora.py \
      --nproc_per_node 1 \
      -- \
      --task train \
      --model-config.model-name-or-path ${MODEL} \
      --model-config.llm-attn-implementation sdpa \
      --model-config.vision-attn-implementation flash_attention_2 \
      --model-config.action-attn-implementation sdpa \
      --optimizer-config.warmup-steps 2 \
      --trainer-config.output-dir ${OUTPUT} \
      --trainer-config.num-train-steps ${NUM_TRAIN_STEPS} \
      --trainer-config.per-device-train-batch-size 1 \
      --trainer-config.gradient-accumulation-steps 1 \
      --trainer-config.save-steps ${SAVE_STEPS} \
      --trainer-config.save-total-limit 5 \
      --trainer-config.no-save-only-model \
      --trainer-config.dataloader-num-workers 2 \
      --trainer-config.dataloader-persistent-workers \
      --trainer-config.dataloader-prefetch-factor 2 \
      --data-config.norm-stats-root ${DATA_ROOT}/norm_stats'"

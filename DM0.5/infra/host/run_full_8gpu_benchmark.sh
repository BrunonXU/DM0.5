#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="docker.1ms.run/dexmal/opendm@sha256:REDACTED_HOST_FINGERPRINT"
DATA_ROOT="/workspace/server/processed/full"
OUTPUT="/workspace/server/checkpoints/benchmark_full_8gpu_500"
SUMMARY="${PROJECT_ROOT}/server/processed/full/conversion_summary.json"

until [[ -f "${SUMMARY}" ]]; do
  sleep 15
done

python3 - "${SUMMARY}" <<'PY'
import json
import sys

summary = json.load(open(sys.argv[1]))
if summary["failed_episode_count"]:
    raise SystemExit(f"conversion failures: {summary['failures']}")
if summary["converted_episode_count"] <= 0:
    raise SystemExit("conversion produced no episodes")
PY

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
      --trainer-config.per-device-train-batch-size 1 \
      --trainer-config.gradient-accumulation-steps 1 \
      --trainer-config.save-steps 250 \
      --trainer-config.save-total-limit 2 \
      --trainer-config.save-only-model \
      --trainer-config.dataloader-num-workers 4 \
      --trainer-config.dataloader-persistent-workers \
      --trainer-config.dataloader-prefetch-factor 2 \
      --data-config.norm-stats-root ${DATA_ROOT}/norm_stats'"

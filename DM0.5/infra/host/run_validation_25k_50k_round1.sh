#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="docker.1ms.run/dexmal/opendm@sha256:REDACTED_HOST_FINGERPRINT"
RESULT_ROOT="${PROJECT_ROOT}/server/evaluations/validation_25k_50k_round1"
MANIFEST="${RESULT_ROOT}/manifest.json"
CONTAINER_MANIFEST="/workspace/server/evaluations/validation_25k_50k_round1/manifest.json"

mkdir -p "${RESULT_ROOT}"
python3 "${PROJECT_ROOT}/g2_sort/validation/build_episode_balanced_manifest.py" \
  --index-cache "${PROJECT_ROOT}/server/processed/full/jsonl/validation/index_cache.json" \
  --samples-per-episode 16 \
  --output "${MANIFEST}"

checkpoints=(25000 30000 35000 40000 45000 50000)
pids=()
for gpu in "${!checkpoints[@]}"; do
  checkpoint="${checkpoints[$gpu]}"
  output="/workspace/server/evaluations/validation_25k_50k_round1/checkpoint-${checkpoint}.json"
  log="${RESULT_ROOT}/checkpoint-${checkpoint}.log"
  sg docker -c "docker run --rm \
    --gpus device=${gpu} \
    --shm-size=16g \
    -e G2_SORT_DATA_ROOT=/workspace/server/processed/full \
    -e PYTHONPATH=/workspace:/workspace/opendm \
    -v ${PROJECT_ROOT}:/workspace \
    -w /workspace \
    ${IMAGE} \
    bash -lc 'source /opt/conda/etc/profile.d/conda.sh && \
      conda activate opendm && \
      python g2_sort/validation/evaluate_real_loss.py \
        --model /workspace/server/checkpoints/dm05_g2_8gpu_50k/checkpoint-${checkpoint} \
        --processor /workspace/server/model_cache/Dexmal/DM05 \
        --norm-stats-root /workspace/server/processed/full/norm_stats \
        --manifest ${CONTAINER_MANIFEST} \
        --batch-size 4 \
        --seed-repeats 3 \
        --seed 20260729 \
        --device cuda:0 \
        --output ${output}'" >"${log}" 2>&1 &
  pids+=("$!")
done

rc=0
for pid in "${pids[@]}"; do
  if ! wait "${pid}"; then
    rc=1
  fi
done
exit "${rc}"

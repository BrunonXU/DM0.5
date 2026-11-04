#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -lt 3 ]]; then
  echo "usage: $0 RESULT_ROOT MANIFEST CHECKPOINT..." >&2
  exit 2
fi
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="docker.1ms.run/dexmal/opendm@sha256:REDACTED_HOST_FINGERPRINT"
RESULT_ROOT="$1"
MANIFEST="$2"
shift 2
checkpoints=("$@")

mkdir -p "${RESULT_ROOT}"
python3 "${PROJECT_ROOT}/g2_sort/validation/build_episode_balanced_manifest.py" \
  --index-cache "${PROJECT_ROOT}/server/processed/full/jsonl/validation/index_cache.json" \
  --samples-per-episode 4 \
  --output "${MANIFEST}"
container_manifest="/workspace/${MANIFEST#${PROJECT_ROOT}/}"

pids=()
for gpu in "${!checkpoints[@]}"; do
  checkpoint="${checkpoints[$gpu]}"
  output="/workspace/${RESULT_ROOT#${PROJECT_ROOT}/}/checkpoint-${checkpoint}.json"
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
      python g2_sort/validation/evaluate_generated_actions.py \
        --model /workspace/server/checkpoints/dm05_g2_8gpu_50k/checkpoint-${checkpoint} \
        --processor /workspace/server/model_cache/Dexmal/DM05 \
        --norm-stats-root /workspace/server/processed/full/norm_stats \
        --manifest ${container_manifest} \
        --batch-size 2 \
        --seed-repeats 2 \
        --seed 20260729 \
        --diffusion-steps 10 \
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

#!/usr/bin/env bash
# 6000D only. Run through the existing docker group; starts no robot process.
set -euo pipefail
task_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
projects=$(cd -- "$task_root/../.." && pwd -P)
bundle=${1:-"$task_root/artifacts/20261016-v1-formal-training/bundle-500"}
name=${2:-f1-toys-v1-formal-500-5step}
gpu=${F1_TOYS_GPU:-1}
steps=${F1_TOYS_STEPS:-5}
port=${F1_TOYS_PORT:-8096}
core=${F1_TOYS_CORE:-/workspace/tasks/F1_ComplexyToys/artifacts/h20-staging/f1-toys-20261016-v1-formal-r3/RLinf}
dataset=${F1_TOYS_WARMUP_DATASET:-/workspace/tasks/F1_ComplexyToys/artifacts/20261016-v1-formal-training/dataset/train}
test -f "$bundle/bundle.json"
digest=$(sha256sum "$bundle/bundle.json" | cut -d' ' -f1)
if docker inspect "$name" >/dev/null 2>&1; then
    test "$(docker inspect -f '{{index .Config.Labels "f1.bundle"}}' "$name")" = "$digest"
    test "$(docker inspect -f '{{index .Config.Labels "f1.steps"}}' "$name")" = "$steps"
    if [[ $(docker inspect -f '{{.State.Running}}' "$name") == true ]]; then
        echo "Already running: $name"; exit 0
    fi
    docker start "$name"; exit 0
fi
case "$bundle" in "$projects"/*) ;; *) echo 'Bundle must be under Projects' >&2; exit 1;; esac
logs="$task_root/artifacts/20261016-v1-eef/service-$name"
mkdir -p "$logs"
bundle_container="/workspace/${bundle#"$projects/"}"
docker run -d --name "$name" --restart unless-stopped --network host \
    --gpus "device=$gpu" --user "$(id -u):$(id -g)" --label "f1.bundle=$digest" --label "f1.steps=$steps" \
    -e USER=user -e PYTHONDONTWRITEBYTECODE=1 -e OMP_NUM_THREADS=4 \
    -e MKL_NUM_THREADS=4 -e OPENBLAS_NUM_THREADS=4 -e TORCH_COMPILE_DISABLE=1 \
    -e TORCHINDUCTOR_CACHE_DIR=/tmp/f1-toys-torch -e NO_ALBUMENTATIONS_UPDATE=1 \
    -e PYTHONPATH=/workspace/tasks/F1_ComplexyToys/src \
    -v "$projects:/workspace:ro" -v "$logs:/logs" \
    --entrypoint /opt/conda/envs/opendm/bin/python \
    sha256:REDACTED_HOST_FINGERPRINT \
    -m f1_toys.server --bundle "$bundle_container" \
    --rlinf-root "$core" --dataset "$dataset" \
    --host 127.0.0.1 --port "$port" --steps "$steps" --log /logs/requests.jsonl
echo "Logs: docker logs -f $name ; $logs/requests.jsonl"

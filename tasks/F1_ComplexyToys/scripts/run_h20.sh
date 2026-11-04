#!/usr/bin/env bash
# Run from a frozen H20 release, in a task-specific tmux session.
set -euo pipefail
task_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
release_root=$(cd -- "$task_root/../.." && pwd -P)
source /home/user/projects/infra/h20/envs/f1-jimu-pi05-v1/activate.sh
export RLINF_ROOT="$release_root/RLinf"
export PYTHONPATH="$task_root/src:$RLINF_ROOT${F1_TOYS_EXTRA_PYTHONPATH:+:$F1_TOYS_EXTRA_PYTHONPATH}"
export F1_TOYS_DATASET=${F1_TOYS_DATASET:-/u01/user/datasets/f1_toys_v1_smoke_20261015/dataset}
export F1_TOYS_TOKENIZER=/u01/user/models/tokenizer-1711547605575873/paligemma_tokenizer.model
: "${F1_TOYS_RUN:?Set a new personal run directory}"
: "${F1_TOYS_GPU_INDEX:?Set a checked, idle physical GPU index}"
export CUDA_VISIBLE_DEVICES="$F1_TOYS_GPU_INDEX"
export NO_ALBUMENTATIONS_UPDATE=1
export OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4
export RAY_ADDRESS=local RAY_USAGE_STATS_ENABLED=0
# Ray uses UNIX sockets with a 107-byte path limit. Keep the root short,
# independent of descriptive run names, and unique for each invocation.
RAY_TMPDIR=$(mktemp -d "/tmp/f1-ray-${UID}-XXXXXX")
export RAY_TMPDIR
export RLINF_CODE_WORKING_DIR=0
export HYDRA_FULL_ERROR=1
mkdir -p "$F1_TOYS_RUN"
cd "$task_root"
python "$task_root/scripts/train_entry.py" --config-path "$task_root/configs" --config-name "${F1_TOYS_CONFIG:-sft_smoke}" "$@"

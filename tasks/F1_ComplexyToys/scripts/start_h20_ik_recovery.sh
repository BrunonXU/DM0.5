#!/usr/bin/env bash
# Independent inference-only service; no robot imports or control output.
set -euo pipefail
source /home/user/projects/infra/h20/envs/f1-jimu-pi05-v1/activate.sh
recovery_run=/u01/user/runs/f1-ik-recovery-20261021
recovery_frozen=/home/user/projects/task-releases/f1-toys-20261020-rtc-h20-v4-fastmath
export CUDA_VISIBLE_DEVICES=2
export PYTHONPATH="$recovery_frozen/src"
export TORCH_COMPILE_DISABLE=1 OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4 NO_ALBUMENTATIONS_UPDATE=1
export TORCHINDUCTOR_CACHE_DIR="$recovery_run/cache/inductor"
export TRITON_CACHE_DIR="$recovery_run/cache/triton"
mkdir -p "$recovery_run/logs"
exec python -u "$recovery_run/ik_recovery_server.py" \
 --recovery-compat "$recovery_frozen/h20_rtc_fast_server.py" \
 --recovery-max-attempts 4 --recovery-budget-ms 400 \
 --recovery-audit-log "$recovery_run/logs/recovery.jsonl" \
 --rtc --rtc-compile-suffix --steps 5 --host 127.0.0.1 --port 8160 \
 --bundle /u01/user/runs/f1-toys-v1-formal-20261016-r3/bundle-50000 \
 --rlinf-root /home/user/projects/task-releases/f1-toys-20261016-v1-formal-r3/RLinf \
 --dataset /u01/user/datasets/f1_toys_v1_formal_20261016/dataset/train \
 --log "$recovery_run/logs/requests.jsonl"

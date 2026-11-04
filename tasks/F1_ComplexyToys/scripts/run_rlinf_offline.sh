#!/usr/bin/env bash
set -euo pipefail
: "${F1_OFFLINE_RUN:?Set the independent run directory}"
F1_OFFLINE_MODE=${1:-baseline}
F1_OFFLINE_ATTEMPT=${F1_OFFLINE_ATTEMPT:-$F1_OFFLINE_MODE}
F1_OFFLINE_CODE=${F1_OFFLINE_CODE:-"$F1_OFFLINE_RUN/code/tasks/F1_ComplexyToys"}
F1_OFFLINE_FROZEN=/home/user/projects/task-releases/f1-toys-20261022-v1-dagger-r3
F1_OFFLINE_BUNDLE=${F1_OFFLINE_BUNDLE:-/u01/user/runs/f1-toys-v1-dagger-20261022-r3/bundle-1000}
F1_OFFLINE_TIMING_PAIRS=${F1_OFFLINE_TIMING_PAIRS:-240}
F1_OFFLINE_GPU=${F1_OFFLINE_GPU:-4}
source /home/user/projects/infra/h20/envs/f1-jimu-pi05-v1/activate.sh
export CUDA_VISIBLE_DEVICES="$F1_OFFLINE_GPU"
export PYTHONPATH="$F1_OFFLINE_FROZEN/tasks/F1_ComplexyToys/src:/u01/user/envs/f1-toys-augmentation-v1:$F1_OFFLINE_CODE/scripts"
export TORCH_COMPILE_DISABLE=1 OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4
export NO_ALBUMENTATIONS_UPDATE=1 PYTHONDONTWRITEBYTECODE=1
export F1_TOYS_TOKENIZER=/u01/user/models/tokenizer-1711547605575873/paligemma_tokenizer.model
export TORCHINDUCTOR_CACHE_DIR="$F1_OFFLINE_RUN/compile-cache" TRITON_CACHE_DIR="$F1_OFFLINE_RUN/triton-cache"
export TORCHINDUCTOR_COMPILE_THREADS=4
trap 'code=$?; printf "%s\n" "$code" > "$F1_OFFLINE_RUN/$F1_OFFLINE_ATTEMPT.exit-code"' EXIT
python - <<'PY'
import os, subprocess, time
target=os.environ['CUDA_VISIBLE_DEVICES']
assert target in ('4','5','6','7')
for attempt in range(10):
    rows=subprocess.check_output(['nvidia-smi','--query-gpu=index,memory.used,utilization.gpu','--format=csv,noheader,nounits'],text=True).splitlines()
    gpu=[r.split(',') for r in rows if r.split(',')[0].strip()==target][0]
    assert int(gpu[1])<100,gpu
    if int(gpu[2])==0: break
    time.sleep(1)
else: raise RuntimeError(f'GPU not idle: {gpu}')
print(f'GPU{target}_IDLE_CHECKED',flush=True)
PY
python -u "$F1_OFFLINE_CODE/scripts/benchmark_rlinf_offline.py" \
 --inputs "$F1_OFFLINE_RUN/inputs" \
 --bundle "$F1_OFFLINE_BUNDLE" \
 --core "$F1_OFFLINE_FROZEN/RLinf" \
 --compat "$F1_OFFLINE_FROZEN/tasks/F1_ComplexyToys/scripts/h20_rtc_fast_server.py" \
 --dataset /u01/user/datasets/f1_toys_v1_dagger_right_only_20261022/dataset/validation \
 --panel /u01/user/runs/f1-toys-stage-sampling-20261022-r3/protocol/panel.json \
 --mode "$F1_OFFLINE_MODE" --timing-pairs "$F1_OFFLINE_TIMING_PAIRS" \
 --output "$F1_OFFLINE_RUN/$F1_OFFLINE_ATTEMPT"

#!/usr/bin/env bash
set -euo pipefail
F1_RLTD_TASK=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
F1_RLTD_RELEASE=$(cd -- "$F1_RLTD_TASK/../.." && pwd -P)
: "${F1_RLTD_RUN_ROOT:?Set a new H20 run root}"
: "${F1_RLTD_RUN_ID:?Set a unique W&B prefix}"
test ! -e "$F1_RLTD_RUN_ROOT"
mkdir -p "$F1_RLTD_RUN_ROOT"
exec > >(tee -a "$F1_RLTD_RUN_ROOT/pipeline.log") 2>&1
trap 'code=$?; printf "%s\n" "$code" > "$F1_RLTD_RUN_ROOT/exit-code.txt"' EXIT
source /home/user/projects/infra/h20/envs/f1-jimu-pi05-v1/activate.sh
export CUDA_VISIBLE_DEVICES=4
export PYTHONPATH="$F1_RLTD_TASK/src"
export F1_RLT_CORE="$F1_RLTD_RELEASE/RLinf"
export F1_TOYS_TOKENIZER=/u01/user/models/tokenizer-1711547605575873/paligemma_tokenizer.model
export OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4
export HTTPS_PROXY=http://127.0.0.1:18096 HTTP_PROXY=http://127.0.0.1:18096
export WANDB_MODE=online NO_ALBUMENTATIONS_UPDATE=1
python - <<'PY'
import subprocess, shutil, wandb
rows = subprocess.check_output(['nvidia-smi', '--query-gpu=index,uuid,memory.used,utilization.gpu', '--format=csv,noheader,nounits'], text=True).splitlines()
gpu = next(r.split(',') for r in rows if int(r.split(',')[0]) == 4)
assert int(gpu[2]) < 100 and int(gpu[3]) == 0, gpu
active = subprocess.check_output(['nvidia-smi', '--query-compute-apps=gpu_uuid,pid', '--format=csv,noheader'], text=True)
assert gpu[1].strip() not in active
assert shutil.disk_usage('/u01').free > 50 * 1024**3
assert wandb.Api(timeout=30).viewer
print('GPU4_IDLE_STORAGE_WANDB_OK', flush=True)
PY
cp "$F1_RLTD_RELEASE/SOURCE_MANIFEST.json" "$F1_RLTD_RUN_ROOT/code-manifest.json"
cp "$F1_RLTD_TASK/configs/rlt_b1250_dagger_success.json" "$F1_RLTD_RUN_ROOT/recipe.json"
python -m pytest -q "$F1_RLTD_TASK/tests/test_rlt_replay.py" \
  "$F1_RLTD_TASK/tests/test_rlt_stage2.py" "$F1_RLTD_TASK/tests/test_rlt_human_replay.py" \
  "$F1_RLTD_TASK/tests/test_rlt_dagger_train.py" > "$F1_RLTD_RUN_ROOT/tests.log" 2>&1
common=(--recipe "$F1_RLTD_TASK/configs/rlt_b1250_dagger_success.json" --core "$F1_RLTD_RELEASE/RLinf")
python -u "$F1_RLTD_TASK/scripts/prepare_rlt_dagger.py" "${common[@]}" \
  --output "$F1_RLTD_RUN_ROOT/cache" --run-id "${F1_RLTD_RUN_ID}cache" > "$F1_RLTD_RUN_ROOT/cache.log" 2>&1
trainer="$F1_RLTD_TASK/scripts/rlt_dagger_train.py"
python -u "$trainer" "${common[@]}" --cache "$F1_RLTD_RUN_ROOT/cache" --phase bc \
  --output "$F1_RLTD_RUN_ROOT/preflight" --steps 2 --run-id "${F1_RLTD_RUN_ID}pre" \
  --job preflight > "$F1_RLTD_RUN_ROOT/preflight.log" 2>&1
python -u "$trainer" "${common[@]}" --cache "$F1_RLTD_RUN_ROOT/cache" --phase bc \
  --output "$F1_RLTD_RUN_ROOT/preflight" --steps 3 \
  --resume "$F1_RLTD_RUN_ROOT/preflight/step-000002.pt" --run-id "${F1_RLTD_RUN_ID}pre" \
  --job preflight > "$F1_RLTD_RUN_ROOT/resume.log" 2>&1
python -u "$trainer" "${common[@]}" --cache "$F1_RLTD_RUN_ROOT/cache" --phase bc \
  --output "$F1_RLTD_RUN_ROOT/bc" --run-id "${F1_RLTD_RUN_ID}bc" \
  --job expert-bc-warmup > "$F1_RLTD_RUN_ROOT/bc.log" 2>&1
printf '%s\n' 'BC_STAGE_FINISHED; REVIEW_REQUIRED_BEFORE_Q_PHASE; NO_ROBOT_DEPLOYMENT'

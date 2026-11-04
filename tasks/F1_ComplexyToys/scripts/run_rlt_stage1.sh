#!/usr/bin/env bash
# Offline-only pipeline; Stage 2 requires real, labelled robot transitions.
set -euo pipefail
TASK_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
RELEASE_ROOT=$(cd -- "$TASK_ROOT/../.." && pwd -P)
: "${F1_RLT_RUN_ROOT:?Set a new H20 run root}"
: "${F1_RLT_RUN_ID:?Set a unique W&B run-id prefix}"
test ! -e "$F1_RLT_RUN_ROOT"
mkdir -p "$F1_RLT_RUN_ROOT"
exec > >(tee -a "$F1_RLT_RUN_ROOT/pipeline.log") 2>&1
trap 'code=$?; printf "%s\n" "$code" > "$F1_RLT_RUN_ROOT/exit-code.txt"' EXIT
source /home/user/projects/infra/h20/envs/f1-jimu-pi05-v1/activate.sh
export CUDA_VISIBLE_DEVICES=4
export F1_TOYS_TOKENIZER=/u01/user/models/tokenizer-1711547605575873/paligemma_tokenizer.model
export PYTHONPATH="$TASK_ROOT/src:$RELEASE_ROOT/RLinf"
export OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4
export HTTPS_PROXY=http://127.0.0.1:18096 HTTP_PROXY=http://127.0.0.1:18096
export WANDB_MODE=online NO_ALBUMENTATIONS_UPDATE=1
python - <<'PY'
import subprocess, shutil, wandb
rows=subprocess.check_output(['nvidia-smi','--query-gpu=index,uuid,memory.used,utilization.gpu','--format=csv,noheader,nounits'],text=True).splitlines()
selected=[r.split(',') for r in rows if int(r.split(',')[0])==4][0]
assert int(selected[2])<100 and int(selected[3])==0, selected
active=subprocess.check_output(['nvidia-smi','--query-compute-apps=gpu_uuid,pid','--format=csv,noheader'],text=True)
assert selected[1].strip() not in active
assert shutil.disk_usage('/u01').free>200*1024**3
assert wandb.Api(timeout=30).viewer
print('GPU4_IDLE_STORAGE_AND_WANDB_OK',flush=True)
PY
cp "$RELEASE_ROOT/SOURCE_MANIFEST.json" "$F1_RLT_RUN_ROOT/code-manifest.json"
cp "$TASK_ROOT/configs/rlt_b1250_frozen.json" "$F1_RLT_RUN_ROOT/recipe.json"
python -m pytest -q "$TASK_ROOT/tests/test_rlt.py" > "$F1_RLT_RUN_ROOT/tests.log" 2>&1
common=(--recipe "$TASK_ROOT/configs/rlt_b1250_frozen.json" --core "$RELEASE_ROOT/RLinf")
python -u "$TASK_ROOT/scripts/rlt_stage1.py" cache "${common[@]}" \
  --output "$F1_RLT_RUN_ROOT/cache" --run-id "${F1_RLT_RUN_ID}cache" > "$F1_RLT_RUN_ROOT/cache.log" 2>&1
python -u "$TASK_ROOT/scripts/rlt_stage1.py" train "${common[@]}" \
  --cache "$F1_RLT_RUN_ROOT/cache" --output "$F1_RLT_RUN_ROOT/preflight" \
  --run-id "${F1_RLT_RUN_ID}pre" --job preflight --steps 2 > "$F1_RLT_RUN_ROOT/preflight.log" 2>&1
python -u "$TASK_ROOT/scripts/rlt_stage1.py" train "${common[@]}" \
  --cache "$F1_RLT_RUN_ROOT/cache" --output "$F1_RLT_RUN_ROOT/preflight" \
  --run-id "${F1_RLT_RUN_ID}pre" --job preflight --steps 3 \
  --resume "$F1_RLT_RUN_ROOT/preflight/step-000002.pt" > "$F1_RLT_RUN_ROOT/resume.log" 2>&1
python -u "$TASK_ROOT/scripts/rlt_stage1.py" train "${common[@]}" \
  --cache "$F1_RLT_RUN_ROOT/cache" --output "$F1_RLT_RUN_ROOT/train" \
  --run-id "${F1_RLT_RUN_ID}main" > "$F1_RLT_RUN_ROOT/train.log" 2>&1
python -u "$TASK_ROOT/scripts/rlt_stage1.py" export "${common[@]}" \
  --cache "$F1_RLT_RUN_ROOT/cache" --training "$F1_RLT_RUN_ROOT/train" \
  --output "$F1_RLT_RUN_ROOT/evaluation" --run-id "${F1_RLT_RUN_ID}eval" > "$F1_RLT_RUN_ROOT/evaluation.log" 2>&1
printf '%s\n' 'RLT_STAGE1_OFFLINE_COMPLETE; ONLINE_RL_NOT_STARTED'

#!/usr/bin/env bash
# H20: queue behind the existing 6000-step pipeline; resume the same online run.
set -euo pipefail
TASK_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
RELEASE_ROOT=$(cd -- "$TASK_ROOT/../.." && pwd -P)
RUN=/u01/user/runs/f1-toys-v1-formal-20261016-r3
exec > >(tee -a "$RUN/extension-50k.log") 2>&1
lockfile="$RUN/extension-50k.lock"
exec 9>"$lockfile"
flock -n 9
test ! -f "$RUN/extension-50k-started.json"
echo 'QUEUED: waiting for the original 6000-step pipeline and phase evaluation to finish.'
while [[ ! -f "$RUN/exit-code.txt" ]]; do
    if ! tmux has-session -t =f1-v1-formal 2>/dev/null; then
        echo 'Original pipeline disappeared without completion receipt'; exit 1
    fi
    sleep 15
done
test "$(cat "$RUN/exit-code.txt")" = 0
trap 'code=$?; printf "%s\n" "$code" > "$RUN/extension-50k-exit-code.txt"' EXIT
source /home/user/projects/infra/h20/envs/f1-jimu-pi05-v1/activate.sh
export F1_TOYS_GPU_INDEX=0,1,2,3,4,5,6,7
export F1_TOYS_CONFIG=sft_eef_v1_formal
export F1_TOYS_DATASET=/u01/user/datasets/f1_toys_v1_formal_20261016/dataset/train
export F1_TOYS_VALIDATION=/u01/user/datasets/f1_toys_v1_formal_20261016/dataset/validation
export F1_TOYS_EXTRA_PYTHONPATH=/u01/user/envs/f1-toys-augmentation-v1
export F1_TOYS_RUN="$RUN/train"
export WANDB_MODE=online WANDB_ENTITY=robotics-example
export WANDB_RUN_ID=f1v1formal20261016main1 WANDB_RESUME=must
export NO_ALBUMENTATIONS_UPDATE=1 OPENBLAS_NUM_THREADS=4 OMP_NUM_THREADS=4 MKL_NUM_THREADS=4
checkpoint="$F1_TOYS_RUN/v1_formal_eef_lora/checkpoints/global_step_6000"
test -s "$checkpoint/actor/rng.pt"
test -s "$checkpoint/actor/model_state_dict/full_weights.pt"
HTTPS_PROXY=http://127.0.0.1:18096 HTTP_PROXY=http://127.0.0.1:18096 python - "$RUN" "$RELEASE_ROOT" <<'PY'
import json,subprocess,sys,time,shutil
from pathlib import Path
import wandb
assert wandb.Api().viewer
assert shutil.disk_usage('/u01').free > 4*1024**4, 'Need 4TiB for retained checkpoints'
for _ in range(30):
    rows=subprocess.check_output(['nvidia-smi','--query-gpu=index,memory.used','--format=csv,noheader,nounits'],text=True).strip().splitlines()
    if len(rows)==8 and all(int(r.split(',')[1]) < 1000 for r in rows):break
    time.sleep(2)
else: raise RuntimeError('GPUs not released; refusing to share unexpectedly')
record=dict(resume_step=6000,target_step=50000,global_batch=16,world_size=8,
    learning_rate_after_6000=1e-6,restart_warmup=False,release=sys.argv[2],
    wandb_run='f1v1formal20261016main1',started_unix=time.time())
(Path(sys.argv[1])/'extension-50k-started.json').write_text(json.dumps(record,indent=2)+'\n')
PY
cp "$RELEASE_ROOT/SOURCE_MANIFEST.json" "$RUN/extension-50k-code-manifest.json"
bash "$TASK_ROOT/scripts/run_h20.sh" runner.max_steps=50000 \
    runner.resume_dir="$checkpoint" +actor.optim.f1_hold_lr_after_schedule=true \
    actor.optim.min_lr=null actor.optim.min_lr_rate=0.01 \
    > "$RUN/train-to-50000.log" 2>&1
echo 'TRAINING_50000_COMPLETE'

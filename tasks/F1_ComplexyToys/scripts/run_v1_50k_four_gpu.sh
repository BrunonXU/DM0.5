#!/usr/bin/env bash
# H20: continue the V1 run from 8000 on physical GPUs 0-3, global batch unchanged.
set -euo pipefail
TASK_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
RELEASE_ROOT=$(cd -- "$TASK_ROOT/../.." && pwd -P)
RUN=/u01/user/runs/f1-toys-v1-formal-20261016-r3
exec > >(tee -a "$RUN/four-gpu-50k-launch.log") 2>&1
exec 9>"$RUN/extension-50k.lock"
flock -n 9
test ! -f "$RUN/four-gpu-50k-started.json"
trap 'code=$?; printf "%s\n" "$code" > "$RUN/four-gpu-50k-exit-code.txt"' EXIT
source /home/user/projects/infra/h20/envs/f1-jimu-pi05-v1/activate.sh
export F1_TOYS_GPU_INDEX=0,1,2,3
export F1_TOYS_CONFIG=sft_eef_v1_formal
export F1_TOYS_DATASET=/u01/user/datasets/f1_toys_v1_formal_20261016/dataset/train
export F1_TOYS_VALIDATION=/u01/user/datasets/f1_toys_v1_formal_20261016/dataset/validation
export F1_TOYS_EXTRA_PYTHONPATH=/u01/user/envs/f1-toys-augmentation-v1
export F1_TOYS_RUN="$RUN/train"
export WANDB_MODE=online WANDB_ENTITY=robotics-example
export WANDB_RUN_ID=f1v1formal20261016main1 WANDB_RESUME=must
export NO_ALBUMENTATIONS_UPDATE=1 OPENBLAS_NUM_THREADS=4 OMP_NUM_THREADS=4 MKL_NUM_THREADS=4
checkpoint="$F1_TOYS_RUN/v1_formal_eef_lora/checkpoints/global_step_8000"
test -s "$checkpoint/actor/rng.pt"
test -s "$checkpoint/actor/model_state_dict/full_weights.pt"
HTTPS_PROXY=http://127.0.0.1:18096 HTTP_PROXY=http://127.0.0.1:18096 python - "$RUN" "$RELEASE_ROOT" <<'PY'
import json, subprocess, sys, time, shutil
from pathlib import Path
import wandb
assert wandb.Api().viewer
assert shutil.disk_usage('/u01').free > 4*1024**4
rows = subprocess.check_output(['nvidia-smi', '--query-gpu=index,memory.used',
                               '--format=csv,noheader,nounits'], text=True).splitlines()
selected = {int(r.split(',')[0]): int(r.split(',')[1]) for r in rows
            if int(r.split(',')[0]) in (0, 1, 2, 3)}
assert len(selected) == 4 and all(v < 1000 for v in selected.values()), selected
record = dict(resume_step=8000, target_step=50000, physical_gpus=[0,1,2,3],
              global_batch=16, micro_batch=2, accumulation=2, world_size=4,
              learning_rate_after_6000=1e-6, restart_warmup=False,
              release=sys.argv[2], wandb_run='f1v1formal20261016main1',
              sampler_epoch=10, sampler_micro_offset=1260,
              bitwise_equivalent_to_eight_gpu=False, started_unix=time.time())
(Path(sys.argv[1])/'four-gpu-50k-started.json').write_text(json.dumps(record, indent=2)+'\n')
PY
cp "$RELEASE_ROOT/SOURCE_MANIFEST.json" "$RUN/four-gpu-50k-code-manifest.json"
bash "$TASK_ROOT/scripts/run_h20.sh" runner.max_steps=50000 \
    runner.resume_dir="$checkpoint" +actor.optim.f1_hold_lr_after_schedule=true \
    actor.optim.min_lr=null actor.optim.min_lr_rate=0.01 \
    +data.f1_resume_eight_to_four=true \
    > "$RUN/train-to-50000-four-gpu.log" 2>&1
echo 'TRAINING_50000_COMPLETE'

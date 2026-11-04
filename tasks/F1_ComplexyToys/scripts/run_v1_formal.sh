#!/usr/bin/env bash
# H20 only: isolated online preflight, resume check, then 2000/4000/6000-step phases.
set -euo pipefail
TASK_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
RELEASE_ROOT=$(cd -- "$TASK_ROOT/../.." && pwd -P)
: "${F1_FORMAL_RUN_ROOT:?Set a new personal run root}"
: "${F1_FORMAL_DATA_ROOT:?Set the verified collection root}"
test ! -e "$F1_FORMAL_RUN_ROOT"
mkdir -p "$F1_FORMAL_RUN_ROOT"
exec > >(tee -a "$F1_FORMAL_RUN_ROOT/pipeline.log") 2>&1
trap 'code=$?; printf "%s\n" "$code" > "$F1_FORMAL_RUN_ROOT/exit-code.txt"' EXIT
source /home/user/projects/infra/h20/envs/f1-jimu-pi05-v1/activate.sh
export F1_TOYS_GPU_INDEX=0,1,2,3,4,5,6,7
export F1_TOYS_CONFIG=sft_eef_v1_formal
export F1_TOYS_DATASET="$F1_FORMAL_DATA_ROOT/train"
export F1_TOYS_VALIDATION="$F1_FORMAL_DATA_ROOT/validation"
export F1_TOYS_TOKENIZER=/u01/user/models/tokenizer-1711547605575873/paligemma_tokenizer.model
export F1_TOYS_EXTRA_PYTHONPATH=/u01/user/envs/f1-toys-augmentation-v1
export PYTHONPATH="$TASK_ROOT/src:$RELEASE_ROOT/RLinf:$F1_TOYS_EXTRA_PYTHONPATH"
export WANDB_MODE=online WANDB_ENTITY=robotics-example
export NO_ALBUMENTATIONS_UPDATE=1 OPENBLAS_NUM_THREADS=4 OMP_NUM_THREADS=4 MKL_NUM_THREADS=4
# Fail before allocating GPUs if online monitoring or the agreed hardware is unavailable.
HTTPS_PROXY=http://127.0.0.1:18096 HTTP_PROXY=http://127.0.0.1:18096 python - <<'PY'
import subprocess,wandb
assert wandb.Api().viewer
rows=subprocess.check_output(['nvidia-smi','--query-gpu=index,memory.used','--format=csv,noheader,nounits'],text=True).strip().splitlines()
assert len(rows)==8 and all(int(r.split(',')[1]) < 1000 for r in rows),rows
print('ONLINE_AUTH_AND_EIGHT_IDLE_GPUS_OK',flush=True)
PY
cp "$F1_FORMAL_DATA_ROOT/manifest.json" "$F1_FORMAL_RUN_ROOT/dataset-manifest.json"
cp "$F1_FORMAL_DATA_ROOT/split.json" "$F1_FORMAL_RUN_ROOT/split.json"
cp "$TASK_ROOT/configs/sft_eef_v1_formal.yaml" "$F1_FORMAL_RUN_ROOT/recipe.yaml"
cp "$RELEASE_ROOT/SOURCE_MANIFEST.json" "$F1_FORMAL_RUN_ROOT/code-manifest.json"
export F1_TOYS_RUN="$F1_FORMAL_RUN_ROOT/preflight"
export WANDB_RUN_ID=${F1_FORMAL_PREFLIGHT_ID:-f1v1formal20261016pre2} WANDB_RESUME=never
bash "$TASK_ROOT/scripts/run_h20.sh" runner.max_steps=20 runner.save_interval=20 runner.val_check_interval=10 \
  runner.logger.experiment_name=v1_formal_preflight > "$F1_FORMAL_RUN_ROOT/preflight.log" 2>&1
export WANDB_RESUME=must
bash "$TASK_ROOT/scripts/run_h20.sh" runner.max_steps=21 runner.save_interval=21 runner.val_check_interval=21 \
  runner.logger.experiment_name=v1_formal_preflight \
  runner.resume_dir="$F1_FORMAL_RUN_ROOT/preflight/v1_formal_preflight/checkpoints/global_step_20" \
  > "$F1_FORMAL_RUN_ROOT/resume-check.log" 2>&1
python "$TASK_ROOT/scripts/check_resume.py" \
  --before "$F1_FORMAL_RUN_ROOT/preflight/v1_formal_preflight/checkpoints/global_step_20/actor" \
  --after "$F1_FORMAL_RUN_ROOT/preflight/v1_formal_preflight/checkpoints/global_step_21/actor" \
  --output "$F1_FORMAL_RUN_ROOT/resume-verification.json" > "$F1_FORMAL_RUN_ROOT/resume-verification.log" 2>&1
export F1_TOYS_RUN="$F1_FORMAL_RUN_ROOT/train"
export WANDB_RUN_ID=${F1_FORMAL_MAIN_ID:-f1v1formal20261016main1} WANDB_RESUME=never
previous=0
for target in 2000 4000 6000; do
    resume_args=()
    if (( previous > 0 )); then
        export WANDB_RESUME=must
        resume_args=("runner.resume_dir=$F1_TOYS_RUN/v1_formal_eef_lora/checkpoints/global_step_$previous")
    fi
    # Optimizer schedule always remains 6000/300 steps, including phased resumes.
    bash "$TASK_ROOT/scripts/run_h20.sh" "runner.max_steps=$target" "${resume_args[@]}" \
      > "$F1_FORMAL_RUN_ROOT/train-to-$target.log" 2>&1
    indices=$(python - <<'PY'
import json,os,numpy as np
from pathlib import Path
rows=[json.loads(l) for l in (Path(os.environ['F1_TOYS_VALIDATION'])/'samples.jsonl').open()]
groups={}
for i,r in enumerate(rows):groups.setdefault(r['episode_id'],[]).append(i)
print(','.join(str(g[j]) for g in groups.values() for j in np.linspace(0,len(g)-1,5,dtype=int)))
PY
)
    CUDA_VISIBLE_DEVICES=0 python "$TASK_ROOT/scripts/evaluate_eef.py" --dataset "$F1_TOYS_VALIDATION" \
      --rlinf-root "$RELEASE_ROOT/RLinf" --adapter \
      --weights "$F1_TOYS_RUN/v1_formal_eef_lora/checkpoints/global_step_$target/actor/model_state_dict/full_weights.pt" \
      --indices "$indices" --output "$F1_FORMAL_RUN_ROOT/generation-$target" \
      > "$F1_FORMAL_RUN_ROOT/generation-$target.log" 2>&1
    # Update summary without advancing the training history step or competing with a live writer.
    HTTPS_PROXY=http://127.0.0.1:18096 HTTP_PROXY=http://127.0.0.1:18096 python - "$target" <<'PY'
import json,os,sys,wandb
from pathlib import Path
step=sys.argv[1]
report=json.loads((Path(os.environ['F1_FORMAL_RUN_ROOT'])/f'generation-{step}'/'report.json').read_text())
run=wandb.Api().run('robotics-example/f1_complexytoys/'+os.environ['WANDB_RUN_ID'])
run.summary['generation_step_'+step]=report['summary']
run.summary.update()
PY
    previous=$target
done
printf 'TRAINING_AND_PHASE_EVALUATIONS_COMPLETE\n'

#!/usr/bin/env bash
# H20: authorized GPUs 0-3; monitored Full128 preflight, resume, export, then 1K review.
set -euo pipefail
TASK_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
RELEASE_ROOT=$(cd -- "$TASK_ROOT/../.." && pwd -P)
: "${F1_FULL_RUN_ROOT:?Set a new run directory}"
: "${F1_FULL_DATA_ROOT:?Set the verified new collection directory}"
test ! -e "$F1_FULL_RUN_ROOT"
mkdir -p "$F1_FULL_RUN_ROOT"
exec > >(tee -a "$F1_FULL_RUN_ROOT/pipeline.log") 2>&1
trap 'code=$?; printf "%s\n" "$code" > "$F1_FULL_RUN_ROOT/exit-code.txt"' EXIT
source /home/user/projects/infra/h20/envs/f1-jimu-pi05-v1/activate.sh
export F1_TOYS_GPU_INDEX=0,1,2,3 F1_TOYS_CONFIG=sft_eef_v1_full
export F1_TOYS_DATASET="$F1_FULL_DATA_ROOT/train" F1_TOYS_VALIDATION="$F1_FULL_DATA_ROOT/validation"
export F1_TOYS_TOKENIZER=/u01/user/models/tokenizer-1711547605575873/paligemma_tokenizer.model
export F1_TOYS_EXTRA_PYTHONPATH=/u01/user/envs/f1-toys-augmentation-v1
export PYTHONPATH="$TASK_ROOT/src:$RELEASE_ROOT/RLinf:$F1_TOYS_EXTRA_PYTHONPATH"
export WANDB_MODE=online WANDB_ENTITY=robotics-example
export NO_ALBUMENTATIONS_UPDATE=1 OPENBLAS_NUM_THREADS=4 OMP_NUM_THREADS=4 MKL_NUM_THREADS=4
export HTTPS_PROXY=http://127.0.0.1:18096 HTTP_PROXY=http://127.0.0.1:18096
python - <<'PY'
import subprocess, wandb, shutil
assert wandb.Api().viewer
rows=subprocess.check_output(['nvidia-smi','--query-gpu=index,memory.used','--format=csv,noheader,nounits'],text=True).splitlines()
chosen={int(r.split(',')[0]):int(r.split(',')[1]) for r in rows if int(r.split(',')[0]) in (0,1,2,3)}
assert len(chosen)==4 and all(v<1000 for v in chosen.values()),chosen
assert shutil.disk_usage('/u01').free>1024**4
print('ONLINE_WANDB_AND_GPUS_0_3_IDLE_OK',flush=True)
PY
cp "$F1_FULL_DATA_ROOT/manifest.json" "$F1_FULL_RUN_ROOT/dataset-manifest.json"
cp "$F1_FULL_DATA_ROOT/split.json" "$F1_FULL_RUN_ROOT/split.json"
cp "$TASK_ROOT/configs/sft_eef_v1_full.yaml" "$F1_FULL_RUN_ROOT/recipe.yaml"
cp "$RELEASE_ROOT/SOURCE_MANIFEST.json" "$F1_FULL_RUN_ROOT/code-manifest.json"
export F1_TOYS_RUN="$F1_FULL_RUN_ROOT/preflight"
export WANDB_RUN_GROUP=00_Engineering_Checks WANDB_JOB_TYPE=preflight
export WANDB_TAGS=F1,V1_FullSFT,Full,B128,Home,TimeMask,preflight
export WANDB_RUN_ID=${F1_FULL_PREFLIGHT_ID:-f1full20261020pre1} WANDB_RESUME=never
bash "$TASK_ROOT/scripts/run_h20.sh" runner.max_steps=2 runner.save_interval=2 runner.val_check_interval=2 \
  runner.logger.experiment_name=V1_FullSFT_GPU_ResumeCheck > "$F1_FULL_RUN_ROOT/preflight.log" 2>&1
export WANDB_RESUME=must
bash "$TASK_ROOT/scripts/run_h20.sh" runner.max_steps=3 runner.save_interval=3 runner.val_check_interval=3 \
  runner.logger.experiment_name=V1_FullSFT_GPU_ResumeCheck \
  runner.resume_dir="$F1_TOYS_RUN/V1_FullSFT_GPU_ResumeCheck/checkpoints/global_step_2" \
  > "$F1_FULL_RUN_ROOT/resume.log" 2>&1
pre="$F1_TOYS_RUN/V1_FullSFT_GPU_ResumeCheck/checkpoints"
python "$TASK_ROOT/scripts/check_resume.py" --before "$pre/global_step_2/actor" \
  --after "$pre/global_step_3/actor" --accumulation 16 \
  --output "$F1_FULL_RUN_ROOT/resume-verification.json" > "$F1_FULL_RUN_ROOT/resume-verification.log" 2>&1
python "$TASK_ROOT/scripts/export_eef.py" --full --checkpoint "$pre/global_step_3/actor" \
  --dataset "$F1_TOYS_DATASET" --rlinf-root "$RELEASE_ROOT/RLinf" --tokenizer "$F1_TOYS_TOKENIZER" \
  --base /u01/user/models/pi05-base-e1b61876289b52fa-openpi-rlinf-fp32 \
  --output "$F1_FULL_RUN_ROOT/preflight-bundle-3" > "$F1_FULL_RUN_ROOT/export-check.log" 2>&1
CUDA_VISIBLE_DEVICES=0 python "$TASK_ROOT/scripts/check_full_bundle.py" \
  --checkpoint "$pre/global_step_3/actor" --bundle "$F1_FULL_RUN_ROOT/preflight-bundle-3" \
  --dataset "$F1_TOYS_DATASET" --rlinf-root "$RELEASE_ROOT/RLinf" \
  --output "$F1_FULL_RUN_ROOT/bundle-verification.json" > "$F1_FULL_RUN_ROOT/bundle-verification.log" 2>&1
python - <<'PY'
import json,os,wandb
from pathlib import Path
root=Path(os.environ['F1_FULL_RUN_ROOT'])
reports=[json.loads(f.read_text()) for f in (root/'preflight').glob('full-parameters-*.json')]
assert reports and all(r['total_parameters']==r['trainable_parameters']==3353433872 for r in reports)
run=wandb.Api().run('robotics-example/f1_complexytoys/'+os.environ['WANDB_RUN_ID'])
for name in ('resume-verification','bundle-verification'):
 report=json.loads((root/(name+'.json')).read_text())
 assert report['result']=='PASS'
 run.summary[name]='PASS'
run.summary.update()
(root/'preflight-passed.json').write_text(json.dumps(dict(result='PASS',trainable_parameters=3353433872,global_batch=128,physical_gpus=[0,1,2,3]))+'\n')
PY
export F1_TOYS_RUN="$F1_FULL_RUN_ROOT/train"
export WANDB_RUN_GROUP=03_V1_FullSFT WANDB_JOB_TYPE=train
export WANDB_TAGS=F1,V1_FullSFT,Full,B128,Home,TimeMask,formal
export WANDB_RUN_ID=${F1_FULL_MAIN_ID:-f1full20261020main1} WANDB_RESUME=never
previous=0
for target in 250 500 625 1000; do
    resume_args=()
    if (( previous > 0 )); then
        export WANDB_RESUME=must
        resume_args=("runner.resume_dir=$F1_TOYS_RUN/V1_FullSFT_B128_Home_TimeMask/checkpoints/global_step_$previous")
    fi
    bash "$TASK_ROOT/scripts/run_h20.sh" "runner.max_steps=$target" "${resume_args[@]}" \
      > "$F1_FULL_RUN_ROOT/train-to-$target.log" 2>&1
    ckpt="$F1_TOYS_RUN/V1_FullSFT_B128_Home_TimeMask/checkpoints/global_step_$target/actor"
    python "$TASK_ROOT/scripts/export_eef.py" --full --checkpoint "$ckpt" --dataset "$F1_TOYS_DATASET" \
      --rlinf-root "$RELEASE_ROOT/RLinf" --tokenizer "$F1_TOYS_TOKENIZER" \
      --base /u01/user/models/pi05-base-e1b61876289b52fa-openpi-rlinf-fp32 \
      --output "$F1_FULL_RUN_ROOT/bundle-$target" > "$F1_FULL_RUN_ROOT/export-$target.log" 2>&1
    indices=$(python - <<'PY'
import json,os,numpy as np
from pathlib import Path
rows=[json.loads(l) for l in (Path(os.environ['F1_TOYS_VALIDATION'])/'samples.jsonl').open()]
groups={}
for i,r in enumerate(rows):groups.setdefault(r['episode_id'],[]).append(i)
chosen={g[j] for g in groups.values() for j in np.linspace(0,len(g)-1,3,dtype=int)}
for pred in (lambda r:r['supervision_phase']=='home',lambda r:r['valid_action_steps']<50):
 chosen.add(next(i for i,r in enumerate(rows) if pred(r)))
print(','.join(map(str,sorted(chosen))))
PY
)
    CUDA_VISIBLE_DEVICES=0 python "$TASK_ROOT/scripts/evaluate_eef.py" --dataset "$F1_TOYS_VALIDATION" \
      --rlinf-root "$RELEASE_ROOT/RLinf" --weights "$F1_FULL_RUN_ROOT/bundle-$target/model.safetensors" \
      --indices "$indices" --output "$F1_FULL_RUN_ROOT/generation-$target" \
      > "$F1_FULL_RUN_ROOT/generation-$target.log" 2>&1
    python - "$target" <<'PY'
import json,os,sys,wandb
from pathlib import Path
step=sys.argv[1]
root=Path(os.environ['F1_FULL_RUN_ROOT'])
report=json.loads((root/f'generation-{step}/report.json').read_text())
run=wandb.Api().run('robotics-example/f1_complexytoys/'+os.environ['WANDB_RUN_ID'])
run.summary['generation_step_'+step]=report['summary']
run.summary.update()
PY
    previous=$target
done
printf '1000-step decision point reached; review before continuing the frozen 2000-step schedule.\n' \
  > "$F1_FULL_RUN_ROOT/REVIEW_AT_1000.txt"

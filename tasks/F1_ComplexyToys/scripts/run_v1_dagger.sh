#!/usr/bin/env bash
# H20: authorized GPUs 4-7; monitored Full128 preflight, resume, export, then 1K review.
set -euo pipefail
TASK_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
RELEASE_ROOT=$(cd -- "$TASK_ROOT/../.." && pwd -P)
: "${F1_DAGGER_RUN_ROOT:?Set a new run directory}"
: "${F1_DAGGER_DATA_ROOT:?Set the verified new collection directory}"
test ! -e "$F1_DAGGER_RUN_ROOT"
mkdir -p "$F1_DAGGER_RUN_ROOT"
exec > >(tee -a "$F1_DAGGER_RUN_ROOT/pipeline.log") 2>&1
trap 'code=$?; printf "%s\n" "$code" > "$F1_DAGGER_RUN_ROOT/exit-code.txt"' EXIT
source /home/user/projects/infra/h20/envs/f1-jimu-pi05-v1/activate.sh
export F1_TOYS_GPU_INDEX=4,5,6,7 F1_TOYS_CONFIG=sft_eef_v1_dagger_full
export F1_TOYS_DATASET="$F1_DAGGER_DATA_ROOT/train" F1_TOYS_VALIDATION="$F1_DAGGER_DATA_ROOT/validation"
export F1_TOYS_TOKENIZER=/u01/user/models/tokenizer-1711547605575873/paligemma_tokenizer.model
export F1_TOYS_EXTRA_PYTHONPATH=/u01/user/envs/f1-toys-augmentation-v1
export PYTHONPATH="$TASK_ROOT/src:$RELEASE_ROOT/RLinf:$F1_TOYS_EXTRA_PYTHONPATH"
export WANDB_MODE=online WANDB_ENTITY=robotics-example
export NO_ALBUMENTATIONS_UPDATE=1 OPENBLAS_NUM_THREADS=4 OMP_NUM_THREADS=4 MKL_NUM_THREADS=4
export HTTPS_PROXY=http://127.0.0.1:18096 HTTP_PROXY=http://127.0.0.1:18096
python - <<'PY'
import subprocess, wandb, shutil
assert wandb.Api().viewer
rows=subprocess.check_output(['nvidia-smi','--query-gpu=index,uuid,memory.used,utilization.gpu','--format=csv,noheader,nounits'],text=True).splitlines()
chosen={int(parts[0]):parts[1:] for row in rows
        if int((parts:=[x.strip() for x in row.split(',')])[0]) in (4,5,6,7)}
assert len(chosen)==4 and all(int(v[1])<100 and int(v[2])==0 for v in chosen.values()),chosen
active=subprocess.check_output(['nvidia-smi','--query-compute-apps=gpu_uuid,pid','--format=csv,noheader'],text=True).splitlines()
uuids={v[0] for v in chosen.values()}
assert not any(row.split(',')[0].strip() in uuids for row in active), 'Reserved GPU has a compute process'
assert shutil.disk_usage('/u01').free>1024**4
print('ONLINE_WANDB_AND_GPUS_4_7_IDLE_OK',flush=True)
PY
read -r F1_TOYS_CONTRACT_SHA256 F1_TOYS_NORM_STATS_SHA256 F1_TOYS_SPLIT_SHA256 < <(python - <<'PYHASH'
import json,os
from pathlib import Path
m=json.loads((Path(os.environ['F1_TOYS_DATASET'])/'manifest.json').read_text())
assert m['formal_training_ready'] and m['input_version']=='right_only_v1'
assert m['sampling']['counts']=={'original_demo':96,'correction':32}
print(m['contract_sha256'],m['norm_stats_sha256'],m['split_sha256'])
PYHASH
)
export F1_TOYS_CONTRACT_SHA256 F1_TOYS_NORM_STATS_SHA256 F1_TOYS_SPLIT_SHA256
cp "$F1_DAGGER_DATA_ROOT/manifest.json" "$F1_DAGGER_RUN_ROOT/dataset-manifest.json"
cp "$F1_DAGGER_DATA_ROOT/split.json" "$F1_DAGGER_RUN_ROOT/split.json"
cp "$TASK_ROOT/configs/sft_eef_v1_dagger_full.yaml" "$F1_DAGGER_RUN_ROOT/recipe.yaml"
cp "$RELEASE_ROOT/SOURCE_MANIFEST.json" "$F1_DAGGER_RUN_ROOT/code-manifest.json"
export F1_TOYS_RUN="$F1_DAGGER_RUN_ROOT/preflight"
export WANDB_RUN_GROUP=00_Engineering_Checks WANDB_JOB_TYPE=preflight
export WANDB_TAGS=F1,V1_DAgger_FullSFT,v1_dagger,right_only,state_mask,left_wrist_mask,dagger_r1_standard,mix75_25,Full,B128,Home,TimeMask,preflight
export WANDB_RUN_ID=${F1_DAGGER_PREFLIGHT_ID:-f1dagger20261021pre1} WANDB_RESUME=never
bash "$TASK_ROOT/scripts/run_h20.sh" runner.max_steps=2 runner.save_interval=2 runner.val_check_interval=2 \
  runner.logger.experiment_name=V1_DAgger_RightOnly_ResumeCheck > "$F1_DAGGER_RUN_ROOT/preflight.log" 2>&1
export WANDB_RESUME=must
bash "$TASK_ROOT/scripts/run_h20.sh" runner.max_steps=3 runner.save_interval=3 runner.val_check_interval=3 \
  runner.logger.experiment_name=V1_DAgger_RightOnly_ResumeCheck \
  runner.resume_dir="$F1_TOYS_RUN/V1_DAgger_RightOnly_ResumeCheck/checkpoints/global_step_2" \
  > "$F1_DAGGER_RUN_ROOT/resume.log" 2>&1
pre="$F1_TOYS_RUN/V1_DAgger_RightOnly_ResumeCheck/checkpoints"
python "$TASK_ROOT/scripts/check_resume.py" --before "$pre/global_step_2/actor" \
  --after "$pre/global_step_3/actor" --accumulation 16 \
  --output "$F1_DAGGER_RUN_ROOT/resume-verification.json" > "$F1_DAGGER_RUN_ROOT/resume-verification.log" 2>&1
python "$TASK_ROOT/scripts/export_eef.py" --full --checkpoint "$pre/global_step_3/actor" \
  --dataset "$F1_TOYS_DATASET" --rlinf-root "$RELEASE_ROOT/RLinf" --tokenizer "$F1_TOYS_TOKENIZER" \
  --base /u01/user/models/pi05-base-e1b61876289b52fa-openpi-rlinf-fp32 \
  --output "$F1_DAGGER_RUN_ROOT/preflight-bundle-3" > "$F1_DAGGER_RUN_ROOT/export-check.log" 2>&1
CUDA_VISIBLE_DEVICES=4 TORCH_COMPILE_DISABLE=1 TORCHINDUCTOR_CACHE_DIR="$F1_DAGGER_RUN_ROOT/compile-cache" python "$TASK_ROOT/scripts/check_full_bundle.py" \
  --checkpoint "$pre/global_step_3/actor" --bundle "$F1_DAGGER_RUN_ROOT/preflight-bundle-3" \
  --dataset "$F1_TOYS_DATASET" --rlinf-root "$RELEASE_ROOT/RLinf" \
  --output "$F1_DAGGER_RUN_ROOT/bundle-verification.json" > "$F1_DAGGER_RUN_ROOT/bundle-verification.log" 2>&1
python - <<'PY'
import json,os,wandb
from pathlib import Path
root=Path(os.environ['F1_DAGGER_RUN_ROOT'])
reports=[json.loads(f.read_text()) for f in (root/'preflight').glob('full-parameters-*.json')]
assert reports and all(r['total_parameters']==r['trainable_parameters']==3353433872 for r in reports)
run=wandb.Api().run('robotics-example/f1_complexytoys/'+os.environ['WANDB_RUN_ID'])
for name in ('resume-verification','bundle-verification'):
 report=json.loads((root/(name+'.json')).read_text())
 assert report['result']=='PASS'
 run.summary[name]='PASS'
run.summary.update()
(root/'preflight-passed.json').write_text(json.dumps(dict(result='PASS',trainable_parameters=3353433872,global_batch=128,physical_gpus=[4,5,6,7]))+'\n')
PY
export F1_TOYS_RUN="$F1_DAGGER_RUN_ROOT/train"
export WANDB_RUN_GROUP=04_RightOnly_FullSFT WANDB_JOB_TYPE=train
export WANDB_TAGS=F1,V1_DAgger_FullSFT,v1_dagger,right_only,state_mask,left_wrist_mask,dagger_r1_standard,mix75_25,Full,B128,Home,TimeMask,formal
export WANDB_RUN_ID=${F1_DAGGER_MAIN_ID:-f1dagger20261021main1} WANDB_RESUME=never
previous=0
for target in 250 500 625 1000; do
    resume_args=()
    if (( previous > 0 )); then
        export WANDB_RESUME=must
        resume_args=("runner.resume_dir=$F1_TOYS_RUN/V1_DAgger_RightOnly_Full_B128_75_25/checkpoints/global_step_$previous")
    fi
    bash "$TASK_ROOT/scripts/run_h20.sh" "runner.max_steps=$target" "${resume_args[@]}" \
      > "$F1_DAGGER_RUN_ROOT/train-to-$target.log" 2>&1
    ckpt="$F1_TOYS_RUN/V1_DAgger_RightOnly_Full_B128_75_25/checkpoints/global_step_$target/actor"
    python "$TASK_ROOT/scripts/export_eef.py" --full --checkpoint "$ckpt" --dataset "$F1_TOYS_DATASET" \
      --rlinf-root "$RELEASE_ROOT/RLinf" --tokenizer "$F1_TOYS_TOKENIZER" \
      --base /u01/user/models/pi05-base-e1b61876289b52fa-openpi-rlinf-fp32 \
      --output "$F1_DAGGER_RUN_ROOT/bundle-$target" > "$F1_DAGGER_RUN_ROOT/export-$target.log" 2>&1
    indices=$(python - <<'PY'
import json,os,numpy as np
from pathlib import Path
rows=[json.loads(l) for l in (Path(os.environ['F1_TOYS_VALIDATION'])/'samples.jsonl').open()]
groups={}
for i,r in enumerate(rows):groups.setdefault(r['episode_id'],[]).append(i)
chosen={g[j] for g in groups.values() for j in np.linspace(0,len(g)-1,3,dtype=int)}
for pred in (lambda r:r['supervision_phase']=='home',lambda r:r['valid_action_steps']<50):
 chosen.add(next(i for i,r in enumerate(rows) if pred(r)))
for phase in ('alignment_insertion','release','withdrawal','native_home','terminal_hold'):
 selected=[i for i,r in enumerate(rows) if r['supervision_phase']==phase]
 if selected:chosen.add(selected[len(selected)//2])
print(','.join(map(str,sorted(chosen))))
PY
)
    CUDA_VISIBLE_DEVICES=4 python "$TASK_ROOT/scripts/evaluate_eef.py" --dataset "$F1_TOYS_VALIDATION" \
      --rlinf-root "$RELEASE_ROOT/RLinf" --weights "$F1_DAGGER_RUN_ROOT/bundle-$target/model.safetensors" \
      --indices "$indices" --output "$F1_DAGGER_RUN_ROOT/generation-$target" \
      > "$F1_DAGGER_RUN_ROOT/generation-$target.log" 2>&1
    python - "$target" <<'PY'
import json,os,sys,wandb
from pathlib import Path
step=sys.argv[1]
root=Path(os.environ['F1_DAGGER_RUN_ROOT'])
report=json.loads((root/f'generation-{step}/report.json').read_text())
run=wandb.Api().run('robotics-example/f1_complexytoys/'+os.environ['WANDB_RUN_ID'])
run.summary['generation_step_'+step]=report['summary']
run.summary.update()
PY
    previous=$target
done
printf '1000-step decision point reached; review before continuing the frozen 2000-step schedule.\n' \
  > "$F1_DAGGER_RUN_ROOT/REVIEW_AT_1000.txt"

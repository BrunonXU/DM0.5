#!/usr/bin/env bash
# Two independent forks of the same new DAgger1K checkpoint, sequential GPUs4-7.
set -euo pipefail
TASK_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
RELEASE_ROOT=$(cd -- "$TASK_ROOT/../.." && pwd -P)
: "${F1_STAGE_RUN_ROOT:?Set a new run directory}"
test ! -e "$F1_STAGE_RUN_ROOT"
mkdir -p "$F1_STAGE_RUN_ROOT"
exec > >(tee -a "$F1_STAGE_RUN_ROOT/pipeline.log") 2>&1
trap 'code=$?; printf "%s\n" "$code" > "$F1_STAGE_RUN_ROOT/exit-code.txt"' EXIT
source /home/user/projects/infra/h20/envs/f1-jimu-pi05-v1/activate.sh
export F1_TOYS_GPU_INDEX=4,5,6,7 F1_TOYS_CONFIG=sft_eef_stage_sampling
export F1_TOYS_DATASET=/u01/user/datasets/f1_toys_v1_dagger_right_only_20261022/dataset/train
export F1_TOYS_VALIDATION=/u01/user/datasets/f1_toys_v1_dagger_right_only_20261022/dataset/validation
export F1_TOYS_TOKENIZER=/u01/user/models/tokenizer-1711547605575873/paligemma_tokenizer.model
export F1_TOYS_EXTRA_PYTHONPATH=/u01/user/envs/f1-toys-augmentation-v1
export PYTHONPATH="$TASK_ROOT/src:$RELEASE_ROOT/RLinf:$F1_TOYS_EXTRA_PYTHONPATH"
export WANDB_MODE=online WANDB_ENTITY=robotics-example
export NO_ALBUMENTATIONS_UPDATE=1 OPENBLAS_NUM_THREADS=4 OMP_NUM_THREADS=4 MKL_NUM_THREADS=4
export HTTPS_PROXY=http://127.0.0.1:18096 HTTP_PROXY=http://127.0.0.1:18096
export TORCHINDUCTOR_CACHE_DIR="$F1_STAGE_RUN_ROOT/compile-cache" TRITON_CACHE_DIR="$F1_STAGE_RUN_ROOT/triton-cache"
export F1_STAGE_PARENT=/u01/user/runs/f1-toys-v1-dagger-20261022-r3/train/V1_DAgger_RightOnly_Full_B128_75_25/checkpoints/global_step_1000
parent_bundle=/u01/user/runs/f1-toys-v1-dagger-20261022-r3/bundle-1000
python - <<'PY'
import subprocess, wandb, shutil
assert wandb.Api().viewer
rows=subprocess.check_output(['nvidia-smi','--query-gpu=index,uuid,memory.used,utilization.gpu','--format=csv,noheader,nounits'],text=True).splitlines()
chosen={int(parts[0]):parts[1:] for row in rows
        if int((parts:=[x.strip() for x in row.split(',')])[0]) in (4,5,6,7)}
assert len(chosen)==4 and all(int(v[1])<100 and int(v[2])==0 for v in chosen.values()), chosen
active=subprocess.check_output(['nvidia-smi','--query-compute-apps=gpu_uuid,pid','--format=csv,noheader'],text=True).splitlines()
assert not any(row.split(',')[0].strip() in {v[0] for v in chosen.values()} for row in active)
assert shutil.disk_usage('/u01').free>1024**4
print('ONLINE_WANDB_AND_GPUS_4_7_IDLE_OK',flush=True)
PY
read -r F1_TOYS_CONTRACT_SHA256 F1_TOYS_NORM_STATS_SHA256 F1_TOYS_SPLIT_SHA256 < <(python - <<'PY'
import json,os
from pathlib import Path
m=json.loads((Path(os.environ['F1_TOYS_DATASET'])/'manifest.json').read_text())
assert m['formal_training_ready'] and m['input_version']=='right_only_v1'
print(m['contract_sha256'],m['norm_stats_sha256'],m['split_sha256'])
PY
)
export F1_TOYS_CONTRACT_SHA256 F1_TOYS_NORM_STATS_SHA256 F1_TOYS_SPLIT_SHA256
cp "$RELEASE_ROOT/SOURCE_MANIFEST.json" "$F1_STAGE_RUN_ROOT/code-manifest.json"
python "$TASK_ROOT/scripts/prepare_stage_sampling.py" --dataset "$(dirname "$F1_TOYS_DATASET")" \
  --output "$F1_STAGE_RUN_ROOT/protocol" > "$F1_STAGE_RUN_ROOT/protocol.log"
export F1_STAGE_FOCUS="$F1_STAGE_RUN_ROOT/protocol/focus.json"
F1_STAGE_FOCUS_SHA256=$(sha256sum "$F1_STAGE_FOCUS" | cut -d' ' -f1)
export F1_STAGE_FOCUS_SHA256
python -m pytest -q "$TASK_ROOT/tests/test_mixture_sampler.py" > "$F1_STAGE_RUN_ROOT/sampler-tests.log"

evaluate() {
  CUDA_VISIBLE_DEVICES=4 python -u "$TASK_ROOT/scripts/evaluate_stage_sampling.py" \
    --dataset "$F1_TOYS_VALIDATION" --panel "$F1_STAGE_RUN_ROOT/protocol/panel.json" \
    --bundle "$1" --core "$RELEASE_ROOT/RLinf" --output "$F1_STAGE_RUN_ROOT/eval-$2" \
    > "$F1_STAGE_RUN_ROOT/eval-$2.log" 2>&1
}

# Weighted fork: actual optimizer/cursor/scheduler preservation, then exact resume.
export F1_TOYS_RUN="$F1_STAGE_RUN_ROOT/preflight" F1_STAGE_EXPERIMENT=ReleaseSampling_ResumeCheck F1_STAGE_MULTIPLIER=4
export WANDB_RUN_GROUP=00_Engineering_Checks WANDB_JOB_TYPE=preflight
export WANDB_TAGS=F1,stage_sampling,release_focus,Full,B128,mix75_25,preflight
export WANDB_RUN_ID=f1stage20261022pre1 WANDB_RESUME=never
bash "$TASK_ROOT/scripts/run_h20.sh" --cfg job --resolve > "$F1_STAGE_RUN_ROOT/resolved-preflight.yaml" 2> "$F1_STAGE_RUN_ROOT/config-check.log"
bash "$TASK_ROOT/scripts/run_h20.sh" runner.max_steps=1002 runner.save_interval=1002 runner.val_check_interval=1002 \
  runner.resume_dir="$F1_STAGE_PARENT" > "$F1_STAGE_RUN_ROOT/preflight.log" 2>&1
pre="$F1_TOYS_RUN/$F1_STAGE_EXPERIMENT/checkpoints"
python "$TASK_ROOT/scripts/check_resume.py" --before "$F1_STAGE_PARENT/actor" --after "$pre/global_step_1002/actor" \
  --additional-steps 2 --accumulation 16 --output "$F1_STAGE_RUN_ROOT/fork-verification.json" > "$F1_STAGE_RUN_ROOT/fork-verification.log" 2>&1
export WANDB_RESUME=must
bash "$TASK_ROOT/scripts/run_h20.sh" runner.max_steps=1003 runner.save_interval=1003 runner.val_check_interval=1003 \
  runner.resume_dir="$pre/global_step_1002" > "$F1_STAGE_RUN_ROOT/resume.log" 2>&1
python "$TASK_ROOT/scripts/check_resume.py" --before "$pre/global_step_1002/actor" --after "$pre/global_step_1003/actor" \
  --accumulation 16 --output "$F1_STAGE_RUN_ROOT/resume-verification.json" > "$F1_STAGE_RUN_ROOT/resume-verification.log" 2>&1
evaluate "$parent_bundle" parent1000

for arm in A B; do
  export F1_STAGE_ARM="$arm" F1_STAGE_EXPERIMENT="V1_DAgger_ReleaseSampling_${arm}_1250"
  export F1_TOYS_RUN="$F1_STAGE_RUN_ROOT/train-$arm"
  if [[ "$arm" == A ]]; then export F1_STAGE_MULTIPLIER=1; else export F1_STAGE_MULTIPLIER=4; fi
  export WANDB_RUN_GROUP=05_DAgger_StageSampling WANDB_JOB_TYPE=train
  export WANDB_TAGS="F1,stage_sampling,release_focus,Full,B128,mix75_25,branch_$arm,parent_dagger1000"
  export WANDB_RUN_ID="f1stage20261022${arm,,}1" WANDB_RESUME=never
  bash "$TASK_ROOT/scripts/run_h20.sh" runner.resume_dir="$F1_STAGE_PARENT" > "$F1_STAGE_RUN_ROOT/train-$arm.log" 2>&1
  python "$TASK_ROOT/scripts/export_eef.py" --full \
    --checkpoint "$F1_TOYS_RUN/$F1_STAGE_EXPERIMENT/checkpoints/global_step_1250/actor" \
    --dataset "$F1_TOYS_DATASET" --rlinf-root "$RELEASE_ROOT/RLinf" --tokenizer "$F1_TOYS_TOKENIZER" \
    --base /u01/user/models/pi05-base-e1b61876289b52fa-openpi-rlinf-fp32 \
    --output "$F1_STAGE_RUN_ROOT/bundle-$arm-1250" > "$F1_STAGE_RUN_ROOT/export-$arm.log" 2>&1
  evaluate "$F1_STAGE_RUN_ROOT/bundle-$arm-1250" "$arm-1250"
  python - <<'PY'
import json,os,wandb
from pathlib import Path
root=Path(os.environ['F1_STAGE_RUN_ROOT']);arm=os.environ['F1_STAGE_ARM']
run=wandb.Api().run('robotics-example/f1_complexytoys/'+os.environ['WANDB_RUN_ID'])
run.summary['stage_panel']=json.loads((root/f'eval-{arm}-1250/report.json').read_text())['summary']
run.summary['stage_panel_parent1000']=json.loads((root/'eval-parent1000/report.json').read_text())['summary']
run.summary['parent_checkpoint']=os.environ['F1_STAGE_PARENT']
run.summary['stage_focus_sha256']=os.environ['F1_STAGE_FOCUS_SHA256']
run.summary.update()
PY
done
python - <<'PY'
import json,os
from pathlib import Path
root=Path(os.environ['F1_STAGE_RUN_ROOT'])
reports={arm:json.loads((root/f'eval-{arm}/report.json').read_text()) for arm in ('parent1000','A-1250','B-1250')}
assert len({r['panel_sha256'] for r in reports.values()})==1
(root/'comparison.json').write_text(json.dumps(reports,indent=2)+'\n')
print('PAIRED_TRAINING_AND_HELDOUT_STAGE_EVALUATION_COMPLETE_NO_DEPLOYMENT',flush=True)
PY

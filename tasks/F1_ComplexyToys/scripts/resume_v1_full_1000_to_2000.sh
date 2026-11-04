#!/usr/bin/env bash
# User authorized physical H20 GPUs 4-7 on 2026-10-21; preserve GPU0-2 inference/IK.
# Default mode is read-only. This script never stops any service or robot process.
set -euo pipefail
mode=${1:---check}
[[ "$mode" == --check || "$mode" == --execute ]] || { echo 'Usage: bash resume_v1_full_1000_to_2000.sh [--check|--execute]' >&2; exit 2; }

export F1_CONT_RELEASE=/home/user/projects/task-releases/f1-toys-20261020-v1-full-r1
export F1_CONT_TASK="$F1_CONT_RELEASE/tasks/F1_ComplexyToys"
export F1_CONT_RUN=/u01/user/runs/f1-toys-v1-full-20261020-r1
export F1_CONT_CHECKPOINT="$F1_CONT_RUN/train/V1_FullSFT_B128_Home_TimeMask/checkpoints/global_step_1000"
export F1_CONT_OUTPUT="$F1_CONT_RUN/continuation-1000-to-2000-gpu4-7-20261021"
source /home/user/projects/infra/h20/envs/f1-jimu-pi05-v1/activate.sh
export F1_TOYS_CONFIG=sft_eef_v1_full F1_TOYS_GPU_INDEX=4,5,6,7
export CUDA_VISIBLE_DEVICES="$F1_TOYS_GPU_INDEX"
export F1_TOYS_RUN="$F1_CONT_RUN/train"
export F1_TOYS_DATASET=/u01/user/datasets/f1_toys_v1_home_mask_20261020/dataset/train
export F1_TOYS_VALIDATION=/u01/user/datasets/f1_toys_v1_home_mask_20261020/dataset/validation
export F1_TOYS_TOKENIZER=/u01/user/models/tokenizer-1711547605575873/paligemma_tokenizer.model
export F1_TOYS_EXTRA_PYTHONPATH=/u01/user/envs/f1-toys-augmentation-v1
export PYTHONPATH="$F1_CONT_TASK/src:$F1_CONT_RELEASE/RLinf:$F1_TOYS_EXTRA_PYTHONPATH"
export WANDB_MODE=online WANDB_ENTITY=robotics-example
export WANDB_RUN_ID=f1full20261020main1 WANDB_RESUME=must
export WANDB_RUN_GROUP=03_V1_FullSFT WANDB_JOB_TYPE=train
export WANDB_TAGS=F1,V1_FullSFT,Full,B128,Home,TimeMask,formal,resume
export HTTPS_PROXY=http://127.0.0.1:18096 HTTP_PROXY=http://127.0.0.1:18096
export NO_ALBUMENTATIONS_UPDATE=1 OPENBLAS_NUM_THREADS=4 OMP_NUM_THREADS=4 MKL_NUM_THREADS=4

python - "$mode" <<'PY'
import hashlib, json, os, shutil, subprocess, sys
from pathlib import Path
import torch, wandb, yaml

task=Path(os.environ['F1_CONT_TASK'])
ckpt=Path(os.environ['F1_CONT_CHECKPOINT'])/'actor'
run=Path(os.environ['F1_CONT_RUN'])
assert Path('/u01').is_mount(), '/u01 is not mounted'
assert shutil.disk_usage('/u01').free > 1024**4, 'Require 1 TiB available before eight checkpoint saves and two exports'
assert not Path(os.environ['F1_CONT_OUTPUT']).exists(), 'Continuation output already exists; inspect before any retry'
assert not any((run/f'bundle-{s}').exists() for s in (1500,2000)), 'Target export already exists'
assert not any(int(p.name.rsplit('_',1)[1])>1000 for p in ckpt.parent.parent.glob('global_step_*')), 'Newer checkpoint exists; do not resume stale step1000'
recipe=task/'configs/sft_eef_v1_full.yaml'
assert hashlib.sha256(recipe.read_bytes()).hexdigest()=='2e76ec1ec0eff320f3468b38db2b6a047fd824b83be72cb61c32af2b2603ea2c'
cfg=yaml.safe_load(recipe.read_text())
assert cfg['actor']['global_batch_size']==128 and cfg['actor']['micro_batch_size']==2
assert cfg['actor']['optim']['total_training_steps']==2000 and cfg['actor']['optim']['lr_warmup_steps']==100
for name in ('data.pt','data_progress.pt','rng.pt','dcp_checkpoint/.metadata'):
    assert (ckpt/name).is_file(), name
data=torch.load(ckpt/'data.pt',map_location='cpu',weights_only=False)
rng=torch.load(ckpt/'rng.pt',map_location='cpu',weights_only=False)
progress=torch.load(ckpt/'data_progress.pt',map_location='cpu',weights_only=True)
assert len(data)==len(rng)==4
assert progress=={'epoch':9,'offset':1258}
assert all(x['_num_yielded']==1258 for x in data)
sys.path.insert(0,str(task/'scripts'))
from check_resume import inspect
saved=inspect(ckpt)
assert saved['optimizer_steps'] and set(saved['optimizer_steps'].values())=={1000.0}
online=wandb.Api().run('robotics-example/f1_complexytoys/'+os.environ['WANDB_RUN_ID'])
assert online.summary.get('_step')==999, 'W&B is not at original 1000-update endpoint; inspect before resuming'
rows=subprocess.check_output(['nvidia-smi','--query-gpu=index,memory.used','--format=csv,noheader,nounits'],text=True).splitlines()
gpu={int(x.split(',')[0]):int(x.split(',')[1]) for x in rows if int(x.split(',')[0]) in (4,5,6,7)}
assert len(gpu)==4
idle=all(v<1000 for v in gpu.values())
print(json.dumps({'mode':sys.argv[1],'resume_step':1000,'target_step':2000,'scheduler_total_steps':2000,'global_batch':128,'world_size':4,'accumulation':16,'data_progress':progress,'optimizer_steps':saved['optimizer_steps'],'wandb_run':online.url,'wandb_last_step':online.summary.get('_step'),'gpu_memory_mib':gpu,'all_reserved_gpus_idle':idle},ensure_ascii=False))
if sys.argv[1]=='--execute':
    assert idle, 'GPU4-7 occupied: do not start or stop anyone else; inspect ownership'
PY

if [[ "$mode" == --check ]]; then
    echo 'READ_ONLY_CHECK_COMPLETE: no training, export or process stop performed.'
    exit 0
fi

exec 9>"$F1_CONT_RUN/continuation-1000-to-2000-gpu4-7.lock"
flock -n 9 || { echo 'Another continuation launcher holds the lock' >&2; exit 1; }
mkdir "$F1_CONT_OUTPUT"
cp -- "${BASH_SOURCE[0]}" "$F1_CONT_OUTPUT/launcher.sh"
exec > >(tee -a "$F1_CONT_OUTPUT/pipeline.log") 2>&1
trap 'code=$?; printf "%s\n" "$code" > "$F1_CONT_OUTPUT/exit-code.txt"' EXIT
bash "$F1_CONT_TASK/scripts/run_h20.sh" \
    runner.max_steps=2000 \
    "runner.resume_dir=$F1_CONT_CHECKPOINT" \
    > "$F1_CONT_OUTPUT/train-to-2000.log" 2>&1
# The unchanged save_interval=125 includes1500 and2000. Export both after training.
for target in 1500 2000; do
    python "$F1_CONT_TASK/scripts/export_eef.py" --full \
        --checkpoint "$F1_TOYS_RUN/V1_FullSFT_B128_Home_TimeMask/checkpoints/global_step_$target/actor" \
        --dataset "$F1_TOYS_DATASET" --rlinf-root "$F1_CONT_RELEASE/RLinf" \
        --tokenizer "$F1_TOYS_TOKENIZER" \
        --base /u01/user/models/pi05-base-e1b61876289b52fa-openpi-rlinf-fp32 \
        --output "$F1_CONT_RUN/bundle-$target" > "$F1_CONT_OUTPUT/export-$target.log" 2>&1
done
printf '%s\n' '2000 training complete;1500 and2000 exported. No deployment. Review stage performance before further training.' \
    > "$F1_CONT_OUTPUT/REVIEW_AT_2000.txt"

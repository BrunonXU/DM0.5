#!/usr/bin/env bash
# Diagnostic only. No service mutation, robot I/O, or model deployment.
set -euo pipefail
TASK_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
RELEASE_ROOT=$(cd -- "$TASK_ROOT/../.." && pwd -P)
: "${F1_FIT_RUN_ROOT:?Set a new diagnostic run directory}"
test ! -e "$F1_FIT_RUN_ROOT"
mkdir -p "$F1_FIT_RUN_ROOT"
exec > >(tee -a "$F1_FIT_RUN_ROOT/pipeline.log") 2>&1
trap 'code=$?; printf "%s\n" "$code" > "$F1_FIT_RUN_ROOT/exit-code.txt"' EXIT
source /home/user/projects/infra/h20/envs/f1-jimu-pi05-v1/activate.sh
export F1_TOYS_GPU_INDEX=4,5,6,7 F1_TOYS_CONFIG=sft_eef_release_fit
export F1_TOYS_DATASET=/u01/user/datasets/f1_toys_release_fit_20261023/dataset/train
export F1_TOYS_VALIDATION=/u01/user/datasets/f1_toys_release_fit_20261023/dataset/validation
export F1_TOYS_TOKENIZER=/u01/user/models/tokenizer-1711547605575873/paligemma_tokenizer.model
export F1_TOYS_EXTRA_PYTHONPATH=/u01/user/envs/f1-toys-augmentation-v1
export PYTHONPATH="$TASK_ROOT/src:$RELEASE_ROOT/RLinf:$F1_TOYS_EXTRA_PYTHONPATH"
export WANDB_MODE=online WANDB_ENTITY=robotics-example
export WANDB_RUN_GROUP=06_LearningChain_Diagnostics WANDB_JOB_TYPE=diagnostic
export WANDB_TAGS=F1,Full,tiny_fit,parent_dagger1000,B16,augmentation_off,release_chain
export WANDB_RUN_ID=f1releasefit20261023r1 WANDB_RESUME=never
export NO_ALBUMENTATIONS_UPDATE=1 OPENBLAS_NUM_THREADS=4 OMP_NUM_THREADS=4 MKL_NUM_THREADS=4
export HTTPS_PROXY=http://127.0.0.1:18096 HTTP_PROXY=http://127.0.0.1:18096
export F1_TOYS_RUN="$F1_FIT_RUN_ROOT/train"
export TORCHINDUCTOR_CACHE_DIR="$F1_FIT_RUN_ROOT/compile-cache" TRITON_CACHE_DIR="$F1_FIT_RUN_ROOT/triton-cache"
python - <<'PY'
import subprocess,wandb,shutil
assert wandb.Api().viewer
rows=subprocess.check_output(['nvidia-smi','--query-gpu=index,uuid,memory.used,utilization.gpu','--format=csv,noheader,nounits'],text=True).splitlines()
chosen={int(parts[0]):parts[1:] for row in rows if int((parts:=[s.strip() for s in row.split(',')])[0]) in (4,5,6,7)}
assert len(chosen)==4 and all(int(v[1])<100 and int(v[2])==0 for v in chosen.values()),chosen
active=subprocess.check_output(['nvidia-smi','--query-compute-apps=gpu_uuid,pid','--format=csv,noheader'],text=True).splitlines()
assert not any(row.split(',')[0].strip() in {v[0] for v in chosen.values()} for row in active)
assert shutil.disk_usage('/u01').free>1024**4
print('ONLINE_WANDB_AND_GPUS_4_7_IDLE_OK',flush=True)
PY
read -r F1_TOYS_CONTRACT_SHA256 F1_TOYS_NORM_STATS_SHA256 F1_TOYS_SPLIT_SHA256 < <(python - <<'PY'
import json,os
from pathlib import Path
m=json.loads((Path(os.environ['F1_TOYS_DATASET'])/'manifest.json').read_text())
assert m['diagnostic_only'] and m['samples']==16
print(m['contract_sha256'],m['norm_stats_sha256'],m['split_sha256'])
PY
)
export F1_TOYS_CONTRACT_SHA256 F1_TOYS_NORM_STATS_SHA256 F1_TOYS_SPLIT_SHA256
cp "$RELEASE_ROOT/SOURCE_MANIFEST.json" "$F1_FIT_RUN_ROOT/code-manifest.json"
cp "$(dirname "$F1_TOYS_DATASET")/protocol.json" "$F1_FIT_RUN_ROOT/protocol.json"
bash "$TASK_ROOT/scripts/run_h20.sh" --cfg job --resolve > "$F1_FIT_RUN_ROOT/resolved.yaml" 2> "$F1_FIT_RUN_ROOT/config-check.log"
python - <<'PY'
import os,numpy as np
from pathlib import Path
from f1_toys.dataset import F1Dataset,collate
from f1_toys.processing import sha256
for split in ('F1_TOYS_DATASET','F1_TOYS_VALIDATION'):
 d=F1Dataset(os.environ[split],training=True)
 assert not d.contract['train_image_augmentation'] and len(d)==16
 for i in range(len(d)):
  obs,actions,mask=d[i]
  assert not actions[~mask].any() and not actions[:,10:].any()
  assert obs['image_mask']['base_0_rgb'] and obs['image_mask']['right_wrist_0_rgb'] and not obs['image_mask']['left_wrist_0_rgb']
 batch=collate([d[0],d[1]])
 assert batch[1].shape==(2,50,32) and batch[2].shape==(2,50)
 print(split,'READER_LABEL_MASK_INPUT_OK',flush=True)
PY
evaluate() {
  CUDA_VISIBLE_DEVICES=4 python -u "$TASK_ROOT/scripts/evaluate_release_fit.py" \
    --dataset /u01/user/datasets/f1_toys_v1_dagger_right_only_20261022/dataset \
    --protocol "$F1_FIT_RUN_ROOT/protocol.json" --weights "$1" \
    --core "$RELEASE_ROOT/RLinf" --output "$F1_FIT_RUN_ROOT/eval-$2" > "$F1_FIT_RUN_ROOT/eval-$2.log" 2>&1
}
evaluate /u01/user/runs/f1-toys-v1-dagger-20261022-r3/bundle-1000/model.safetensors parent1000
bash "$TASK_ROOT/scripts/run_h20.sh" > "$F1_FIT_RUN_ROOT/train.log" 2>&1
evaluate "$F1_TOYS_RUN/ReleaseChain_TinyFit_16_B16/checkpoints/global_step_100/actor/model_state_dict/full_weights.pt" fit100
python - <<'PY'
import os,json
from pathlib import Path
root=Path(os.environ['F1_FIT_RUN_ROOT'])
results={name:json.loads((root/f'eval-{name}/report.json').read_text()) for name in ('parent1000','fit100')}
assert len({r['protocol_sha256'] for r in results.values()})==1
(root/'comparison.json').write_text(json.dumps(results,indent=2)+'\n')
print('TINY_FIT_100_AND_PAIRED_EVAL_COMPLETE_NO_DEPLOYMENT',flush=True)
PY

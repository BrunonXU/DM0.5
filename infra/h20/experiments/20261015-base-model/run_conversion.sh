#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
[[ $(id -un) == user ]] && mountpoint -q /u01
logdir=/u01/user/runs/bootstrap-20261015/pi05-base-conversion
mkdir -p "$logdir"
exec >> "$logdir/conversion.log" 2>&1
trap 'printf "FAILED line=%s code=%s\n" "$LINENO" "$?" > "$logdir/conversion.status"' ERR
printf 'RUNNING\n' > "$logdir/conversion.status"
export CUDA_VISIBLE_DEVICES=''
export JAX_PLATFORMS=cpu
export OMP_NUM_THREADS=4
export MKL_NUM_THREADS=4
export PYTHONNOUSERSITE=1
unset PYTHONPATH PYTHONHOME
/u01/user/envs/f1-jimu-pi05-v1/bin/python \
  /home/user/projects/infra/h20/experiments/20261015-base-model/convert_base_only.py \
  --rlinf-root /home/user/projects/vendor/RLinf-d00fe72060a5 \
  --source-provenance /home/user/projects/infra/h20/experiments/20261015-bootstrap/bootstrap-inputs.json \
  --input /u01/user/models/pi05-base-e1b61876289b52fa \
  --output /u01/user/models/pi05-base-e1b61876289b52fa-openpi-rlinf-fp32
printf 'PASS\n' > "$logdir/conversion.status"

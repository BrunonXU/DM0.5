# Explicit task environment for the H20 personal account. Source from Bash.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    echo 'Source this file from an H20 Bash shell.' >&2
    exit 2
fi
if [[ "$(id -un)" != user ]] || ! mountpoint -q /u01; then
    echo 'Expected H20 personal account and mounted /u01.' >&2
    return 2
fi
if [[ ! -x /u01/user/envs/f1-jimu-pi05-v1/bin/python ]]; then
    echo 'Training environment has not been installed.' >&2
    return 2
fi
unset PYTHONPATH PYTHONHOME
export PYTHONNOUSERSITE=1
source /home/user/projects/infra/h20/scripts/job_env.sh f1-jimu-pi05-v1 || return
source /u01/user/envs/f1-jimu-pi05-v1/bin/activate || return
source /u01/user/tools/ffmpeg-4.4.2-jammy/activate.sh || return
export OPENPI_DATA_HOME=/u01/user/cache/openpi
export UV_CACHE_DIR=/u01/user/cache/uv
# Every training run is tracked. Override explicitly only for diagnosed outages.
export WANDB_MODE="${WANDB_MODE:-online}"
export TOKENIZERS_PARALLELISM=false
export RAY_USAGE_STATS_ENABLED=0
# PyTorch owns training GPUs; JAX is retained for CPU transforms/conversion.
export JAX_PLATFORMS=cpu
export XLA_PYTHON_CLIENT_PREALLOCATE=false
# Set CUDA_VISIBLE_DEVICES and distributed rendezvous ports per run, after
# coordinating with the other user. This file does not reserve any GPU.

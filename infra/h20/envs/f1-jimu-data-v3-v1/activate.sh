# Explicit CPU-only LeRobot v3 data environment; source from H20 Bash.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    echo 'Source this file from an H20 Bash shell.' >&2
    exit 2
fi
if [[ "$(id -un)" != user ]] || ! mountpoint -q /u01; then
    echo 'Expected H20 personal account and mounted /u01.' >&2
    return 2
fi
if [[ ! -x /u01/user/envs/f1-jimu-data-v3-v1/bin/python ]]; then
    echo 'Data environment has not been installed.' >&2
    return 2
fi
unset PYTHONPATH PYTHONHOME
export PYTHONNOUSERSITE=1
source /home/user/projects/infra/h20/scripts/job_env.sh f1-jimu-data-v3-v1 || return
source /u01/user/envs/f1-jimu-data-v3-v1/bin/activate || return
source /u01/user/tools/ffmpeg-4.4.2-jammy/activate.sh || return
export CUDA_VISIBLE_DEVICES=''
export WANDB_MODE=offline
export TOKENIZERS_PARALLELISM=false
# This profile does not modify any original dataset. Select separate output
# paths for processed candidates and maintain the task's training_ready gate.

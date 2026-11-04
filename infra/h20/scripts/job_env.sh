# Source in an H20 Bash job shell: source /path/to/job_env.sh ENVIRONMENT_TAG
# Does not install software, change .bashrc, allocate GPUs, or set global limits.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    echo 'Use: source /home/user/projects/infra/h20/scripts/job_env.sh ENVIRONMENT_TAG' >&2
    exit 2
fi

h20_prepare_job_environment() {
    local environment_tag="${1:-}"
    local data_root='/u01/user'
    if [[ ! "$environment_tag" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
        echo 'Supply a simple environment tag, e.g. demo-env-v1' >&2
        return 2
    fi
    if [[ "$(id -un)" != 'user' ]] || ! mountpoint -q /u01 || [[ ! -O "$data_root" || ! -w "$data_root" ]]; then
        echo 'Run in the H20 personal account, with /u01 mounted and personal data root writable.' >&2
        return 2
    fi
    (
        umask 077
        mkdir -p "$data_root/cache/huggingface" "$data_root/cache/pip" \
            "$data_root/cache/xdg" "$data_root/cache/torch" \
            "$data_root/cache/torch-extensions/$environment_tag" \
            "$data_root/cache/triton/$environment_tag" \
            "$data_root/cache/cuda/$environment_tag" "$data_root/staging/tmp"
    ) || return
    export HF_HOME="$data_root/cache/huggingface"
    export PIP_CACHE_DIR="$data_root/cache/pip"
    export XDG_CACHE_HOME="$data_root/cache/xdg"
    export TORCH_HOME="$data_root/cache/torch"
    export TORCH_EXTENSIONS_DIR="$data_root/cache/torch-extensions/$environment_tag"
    export TRITON_CACHE_DIR="$data_root/cache/triton/$environment_tag"
    export CUDA_CACHE_PATH="$data_root/cache/cuda/$environment_tag"
    export TMPDIR="$data_root/staging/tmp"
}

h20_prepare_job_environment "$@"

#!/usr/bin/env bash
# Installation-phase record for the 20261015 bootstrap, not a frozen rebuild.
# Requires pre-created uv, Python, venvs, source and staging inputs.
# Re-resolves dependencies and changes the named environment if run again.
# No sudo, system installs, or shell rc edits.
set -Eeuo pipefail
umask 077
base=/u01/user
if [[ $(id -un) != user ]] || ! mountpoint -q /u01 || [[ ! -O "$base" || ! -w "$base" ]]; then
    echo 'Expected H20 personal account and owned writable data root on mounted /u01.' >&2
    exit 2
fi
stage=$base/staging/bootstrap-20261015
logdir=$base/runs/bootstrap-20261015
envdir=$base/envs/f1-jimu-pi05-v1
role=${1:-train}
requirements=$stage/requirements.in
lockfile=$stage/requirements.lock
if [[ "$role" == data ]]; then
    envdir=$base/envs/f1-jimu-data-v3-v1
    logdir=$base/runs/bootstrap-20261015-data
    requirements=$stage/data-requirements.in
    lockfile=$stage/data-requirements.lock
elif [[ "$role" != train ]]; then
    echo 'Expected train or data' >&2
    exit 2
fi
src=/home/user/projects/vendor/RLinf-d00fe72060a5
uv=$base/tools/uv-0.13.1
export UV_CACHE_DIR=$base/cache/uv
export TMPDIR=$base/staging/tmp
export PYTHONNOUSERSITE=1
unset PYTHONPATH PYTHONHOME VIRTUAL_ENV
# Measured corporate download route, scoped to this process only.
export HTTPS_PROXY=http://192.0.2.34:3128
export HTTP_PROXY=$HTTPS_PROXY
export https_proxy=$HTTPS_PROXY
export http_proxy=$HTTPS_PROXY
export NO_PROXY=localhost,127.0.0.1
export no_proxy=$NO_PROXY
unset ALL_PROXY all_proxy
export UV_HTTP_TIMEOUT=90
export UV_CONCURRENT_DOWNLOADS=4
mkdir -p "$logdir" "$UV_CACHE_DIR" "$TMPDIR"
trap 'printf "FAILED line=%s code=%s\n" "$LINENO" "$?" > "$logdir/install.status"' ERR
printf 'RESOLVING\n' > "$logdir/install.status"
"$uv" pip compile "$requirements" --python "$envdir/bin/python" \
  --no-config --default-index https://pypi.tuna.tsinghua.edu.cn/simple \
  --only-binary av --generate-hashes --output-file "$lockfile" \
  --no-progress
printf 'INSTALLING\n' > "$logdir/install.status"
"$uv" pip install --python "$envdir/bin/python" --no-config \
  --default-index https://pypi.tuna.tsinghua.edu.cn/simple \
  --only-binary av --link-mode copy --require-hashes -r "$lockfile" --no-progress
if [[ "$role" == train ]]; then
    printf 'INSTALLING_RLINF_SOURCE\n' > "$logdir/install.status"
    "$uv" pip install --python "$envdir/bin/python" --no-config --no-deps \
      --no-build-isolation -e "$src" --no-progress
fi
"$uv" pip freeze --python "$envdir/bin/python" > "$logdir/pip-freeze.txt"
printf 'INSTALLED_PENDING_SMOKE\n' > "$logdir/install.status"

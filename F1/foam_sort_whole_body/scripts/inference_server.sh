#!/usr/bin/env bash
set -eo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
projects_root="$(cd -- "$task_root/../.." && pwd -P)"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$task_root/models/DM0.5:$projects_root/DM0.5/opendm${PYTHONPATH:+:$PYTHONPATH}"
exec "${F1_INFERENCE_PYTHON:-python3}" -m f1_inference "${@:-inspect}"

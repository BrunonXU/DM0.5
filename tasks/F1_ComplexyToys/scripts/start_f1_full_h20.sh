#!/usr/bin/env bash
# Compatibility alias only. Catalog owns accepted model names and parameters.
set -euo pipefail
checkpoint=${1:?Usage: start_f1_toys_full_h20.sh STEP [runtime options]}
shift
exec python3 "${F1_INFERENCE_ENTRY:-/home/user/Bruno/DAgger-runtime/inference-current/inference_runtime.py}" --model "full${checkpoint}" "$@"

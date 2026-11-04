#!/usr/bin/env bash
# The catalog selects the HIL baseline; genuine Enter + START remains required.
set -euo pipefail
if [[ ${1:-} == policy ]]; then shift; fi
exec python3 "${F1_INFERENCE_ENTRY:-/home/user/Bruno/DAgger-runtime/inference-current/inference_runtime.py}" --mode hil "$@"

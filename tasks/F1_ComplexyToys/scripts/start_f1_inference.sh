#!/usr/bin/env bash
set -euo pipefail
exec python3 "${F1_INFERENCE_ENTRY:-/home/user/Bruno/DAgger-runtime/inference-current/inference_runtime.py}" "$@"

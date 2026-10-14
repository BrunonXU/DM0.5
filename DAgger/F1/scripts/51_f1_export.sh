#!/usr/bin/env bash
set -euo pipefail
DAGGER_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${DAGGER_ROOT}/src:${DAGGER_ROOT}/../shared/src${PYTHONPATH:+:${PYTHONPATH}}"
exec "${F1_EXPORT_PYTHON:-python3}" -m f1dagger.export "$@"

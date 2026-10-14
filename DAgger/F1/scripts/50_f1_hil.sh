#!/usr/bin/env bash
set -euo pipefail
DAGGER_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${F1_DAGGER_ENV:-}" ]]; then
  set -a
  source "${F1_DAGGER_ENV}"
  set +a
fi
: "${F1_INFRA_ROOT:?Set F1_INFRA_ROOT to the selected F1-infra release}"
: "${F1_PROFILE_PATH:?Set F1_PROFILE_PATH to the current verified machine profile}"
export PYTHONPATH="${DAGGER_ROOT}/src:${DAGGER_ROOT}/../shared/src:${F1_INFRA_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
exec "${F1_DAGGER_PYTHON:-python3}" -m f1dagger "$@"

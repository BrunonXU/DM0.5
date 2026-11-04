#!/usr/bin/env bash
set -uo pipefail
: "${F1_OFFLINE_RUN:?}"
export F1_OFFLINE_CODE="$F1_OFFLINE_RUN/code-v4/tasks/F1_ComplexyToys"
runner="$F1_OFFLINE_CODE/scripts/run_rlinf_offline.sh"
if [[ ${1:?} == prefix ]]; then
  export F1_OFFLINE_GPU=4
  for mode in prefix_compile prefix_constants prefix_graph; do
    export F1_OFFLINE_ATTEMPT="$mode-r2"
    bash "$runner" "$mode" > "$F1_OFFLINE_RUN/$F1_OFFLINE_ATTEMPT.log" 2>&1
  done
else
  export F1_OFFLINE_GPU=5
  for checkpoint in parent A B; do
    export F1_OFFLINE_ATTEMPT="inactive-cache-$checkpoint-r2"
    if [[ $checkpoint == parent ]]; then
      export F1_OFFLINE_BUNDLE=/u01/user/runs/f1-toys-v1-dagger-20261022-r3/bundle-1000
    else
      export F1_OFFLINE_BUNDLE="/u01/user/runs/f1-toys-stage-sampling-20261022-r3/bundle-$checkpoint-1250"
    fi
    bash "$runner" inactive_cache > "$F1_OFFLINE_RUN/$F1_OFFLINE_ATTEMPT.log" 2>&1
  done
fi

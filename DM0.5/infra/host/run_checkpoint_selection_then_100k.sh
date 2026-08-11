#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/home/xza_100850113/projects/DM0.5"
ROUND1="${PROJECT_ROOT}/server/evaluations/validation_25k_50k_round1"
ROUND2="${PROJECT_ROOT}/server/evaluations/validation_25k_50k_round2"
ACTIONS="${PROJECT_ROOT}/server/evaluations/validation_25k_50k_action_finalists"
PIPELINE_LOG="${PROJECT_ROOT}/server/evaluations/checkpoint_selection_pipeline.log"

exec > >(tee -a "${PIPELINE_LOG}") 2>&1
printf 'pipeline_start=%s\n' "$(date --iso-8601=seconds)"

while [[ ! -f "${ROUND1}/run_status.log" ]]; do
  sleep 30
done
if ! grep -q 'rc=0' "${ROUND1}/run_status.log"; then
  echo "Round 1 failed; refusing later evaluation and training." >&2
  exit 1
fi
python3 "${PROJECT_ROOT}/g2_sort/validation/summarize_loss_results.py" \
  --result-dir "${ROUND1}" --top-k 3 --output "${ROUND1}/summary.json"
mapfile -t round2_checkpoints < <(
  python3 -c 'import json,sys; print(*json.load(open(sys.argv[1]))["selected_checkpoints"], sep="\n")' \
    "${ROUND1}/summary.json"
)

"${PROJECT_ROOT}/infra/host/run_validation_loss_candidates.sh" \
  "${ROUND2}" "${ROUND2}/manifest.json" 32 5 "${round2_checkpoints[@]}"
python3 "${PROJECT_ROOT}/g2_sort/validation/summarize_loss_results.py" \
  --result-dir "${ROUND2}" --top-k 2 --output "${ROUND2}/summary.json"
mapfile -t action_checkpoints < <(
  python3 -c 'import json,sys; print(*json.load(open(sys.argv[1]))["selected_checkpoints"], sep="\n")' \
    "${ROUND2}/summary.json"
)

"${PROJECT_ROOT}/infra/host/run_generated_action_candidates.sh" \
  "${ACTIONS}" "${ACTIONS}/manifest.json" "${action_checkpoints[@]}"
python3 "${PROJECT_ROOT}/g2_sort/validation/select_final_checkpoint.py" \
  --round1-summary "${ROUND1}/summary.json" \
  --round2-summary "${ROUND2}/summary.json" \
  --action-dir "${ACTIONS}" \
  --output-json "${ACTIONS}/decision.json" \
  --output-md "${PROJECT_ROOT}/docs/13_DM05_G2_CHECKPOINT_VALIDATION_AND_SELECTION_DECISION.md"

printf 'selection_complete=%s\n' "$(date --iso-8601=seconds)"
printf 'starting_50k_to_100k=%s\n' "$(date --iso-8601=seconds)"
if tmux has-session -t dm05-50k-to-100k 2>/dev/null; then
  echo "Refusing to replace existing tmux dm05-50k-to-100k" >&2
  exit 1
fi
tmux new-session -d -s dm05-50k-to-100k -c "${PROJECT_ROOT}" \
  './infra/host/run_formal_8gpu_50k_to_100k.sh; rc=$?; printf "training_exit=%s rc=%s\n" "$(date --iso-8601=seconds)" "$rc" | tee -a server/runs/formal_50k_to_100k/run_status.log; exec bash'
printf 'training_tmux_started=%s session=dm05-50k-to-100k\n' \
  "$(date --iso-8601=seconds)"

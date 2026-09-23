#!/usr/bin/env bash
set -euo pipefail
task_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
audit_python="${F1_AUDIT_PYTHON:-/tmp/f1-foam-audit-venv/bin/python}"
output_dir="$task_dir/results/cleaning_v1"
source_dir="/mnt/dm05_nvme/dm05/datasets/f1_foam_sort/raw"
mkdir -p "$output_dir"
exec 9>"$output_dir/pipeline.lock"
flock -n 9 || { echo 'cleaning_v1 已有管线运行。' >&2; exit 1; }
case "${1:-all}" in
  all)
    "$audit_python" "$task_dir/scripts/extract_cleaning_signals.py" --source "$source_dir" --output "$output_dir/signals" --workers 2
    "$audit_python" "$task_dir/scripts/index_cleaning_cameras.py" --source "$source_dir" --output "$output_dir/cameras" --workers 2
    "$audit_python" "$task_dir/scripts/build_cleaning_dataset.py" --output "$output_dir"
    ;;
  assemble)
    "$audit_python" "$task_dir/scripts/build_cleaning_dataset.py" --output "$output_dir"
    ;;
  validate) ;;
  *) echo '用法：run_cleaning_v1.sh {all|assemble|validate}' >&2; exit 2 ;;
esac
exec "$audit_python" "$task_dir/scripts/validate_cleaning_dataset.py" --output "$output_dir"

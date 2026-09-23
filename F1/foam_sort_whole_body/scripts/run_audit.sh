#!/usr/bin/env bash
set -euo pipefail
task_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
audit_python="${F1_AUDIT_PYTHON:-/tmp/f1-foam-audit-venv/bin/python}"
source_dir="${F1_DATA_ROOT:-/mnt/dm05_nvme/dm05/datasets/f1_foam_sort/raw}"
output_dir="${F1_AUDIT_OUTPUT:-$task_dir/results/20260914}"
if [[ ! -x "$audit_python" ]]; then
  echo '请先准备 requirements-audit.txt 中的独立环境，并设置 F1_AUDIT_PYTHON。' >&2
  exit 1
fi
case "${1:-metadata}" in
  metadata) exec "$audit_python" "$task_dir/scripts/audit_metadata.py" --source "$source_dir" --output "$output_dir/metadata_audit" ;;
  mcap) exec "$audit_python" "$task_dir/scripts/audit_mcap.py" --root "$source_dir" --out "$output_dir/mcap_audit" --workers 4 ;;
  cameras) exec "$audit_python" "$task_dir/scripts/audit_camera_timestamps.py" --source "$source_dir" --output "$output_dir/camera_timestamps" --workers 4 ;;
  previews) exec "$audit_python" "$task_dir/scripts/preview_episodes.py" --source "$source_dir" --output "$output_dir/previews" ;;
  screening) exec "$audit_python" "$task_dir/scripts/build_review_queue.py" --audit-root "$output_dir" ;;
  videos) exec "$audit_python" "$task_dir/scripts/render_three_camera_videos.py" --source "$source_dir" --output "$output_dir/videos" ;;
  *) echo '用法：run_audit.sh {metadata|mcap|cameras|previews|screening|videos}' >&2; exit 2 ;;
esac

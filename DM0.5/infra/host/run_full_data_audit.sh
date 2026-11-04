#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# The official training image intentionally lacks h5py. This project image is
# used only for H5/video preprocessing; model training remains on the pinned
# official Dexmal image in run_full_8gpu_benchmark.sh.
IMAGE="opendm:g2-34d-fa2"
REPORTS=(
  "${PROJECT_ROOT}/server/runs/data_extract/record.extract.json"
  "${PROJECT_ROOT}/server/runs/data_extract/record_20260409.extract.json"
  "${PROJECT_ROOT}/server/runs/data_extract/record_20260413.extract.json"
)

while :; do
  ready=true
  for report in "${REPORTS[@]}"; do
    [[ -f "${report}" ]] || ready=false
  done
  "${ready}" && break
  sleep 10
done

python3 - "${REPORTS[@]}" <<'PY'
import json
import sys

for path in sys.argv[1:]:
    report = json.load(open(path))
    if report["failure_count"]:
        raise SystemExit(f"extraction failed: {path}: {report['failures']}")
PY

sg docker -c "docker run --rm \
  --shm-size=16g \
  -e PYTHONPATH=/workspace:/workspace/opendm \
  -v ${PROJECT_ROOT}:/workspace \
  -w /workspace \
  ${IMAGE} \
  bash -lc 'source /opt/conda/etc/profile.d/conda.sh && \
    conda activate opendm && \
    python -m g2_sort.data_tools.audit_manifest \
      server/processed/manifests/all_archives.precheck.json \
      server/staged_episodes/full/episodes \
      server/processed/audits/full \
      --workers 24 && \
    python -m g2_sort.data_tools.build_split \
      server/processed/audits/full \
      --output server/processed/manifests/full_split.json \
      --train-count 800 \
      --seed dm05-g2-v1 && \
    python -m g2_sort.data_tools.convert_split_parallel \
      server/processed/manifests/full_split.json \
      server/staged_episodes/full/episodes \
      server/processed/audits/full \
      server/processed/full \
      --workers 24 \
      --fps 30'"

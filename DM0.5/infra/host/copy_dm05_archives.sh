#!/usr/bin/env bash
set -euo pipefail

# Copy the three frozen G2 source archives from a read-only SMB mount to the
# project NVMe. Re-running the script safely resumes partial files.

dm05_project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
dm05_source_root="${1:-/mnt/dm05_source/具身/数采数据/sort_g2}"
dm05_destination="${2:-${dm05_project_root}/server/raw_archives}"
dm05_log_dir="${dm05_project_root}/server/runs/data_transfer"

dm05_archives=(
    "record.tar.gz"
    "record_20260409.zip"
    "record_20260413.tar.gz"
)

dm05_expected_sizes=(
    "50394926225"
    "193356130953"
    "215996415546"
)

mkdir -p "${dm05_destination}" "${dm05_log_dir}"

for dm05_index in "${!dm05_archives[@]}"; do
    dm05_archive="${dm05_archives[${dm05_index}]}"
    dm05_expected_size="${dm05_expected_sizes[${dm05_index}]}"
    dm05_source="${dm05_source_root}/${dm05_archive}"
    dm05_target="${dm05_destination}/${dm05_archive}"

    if [[ "$(stat -c '%s' "${dm05_source}")" != "${dm05_expected_size}" ]]; then
        echo "Unexpected source size for ${dm05_archive}" >&2
        exit 1
    fi

    rsync \
        --human-readable \
        --info=progress2 \
        --partial \
        --append-verify \
        --no-perms \
        --no-owner \
        --no-group \
        --log-file="${dm05_log_dir}/${dm05_archive}.rsync.log" \
        "${dm05_source}" \
        "${dm05_destination}/"

    if [[ "$(stat -c '%s' "${dm05_target}")" != "${dm05_expected_size}" ]]; then
        echo "Unexpected destination size for ${dm05_archive}" >&2
        exit 1
    fi
    chmod a-w "${dm05_target}"
done

echo "All DM0.5 source archives copied, size-verified, and marked read-only."

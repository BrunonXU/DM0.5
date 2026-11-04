#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECTS_DIR="$(cd -- "${SCRIPT_DIR}/../../.." && pwd)"
# Fresh clones keep their submodule in Projects; this 6000D checkout keeps it in
# the sibling Libraries worktree. An explicit OPENPI_ROOT overrides either.
if [[ -z "${OPENPI_ROOT:-}" ]]; then
  OPENPI_ROOT="${PROJECTS_DIR}/PI0.5"
  if [[ ! -d "${OPENPI_ROOT}" && -d "${PROJECTS_DIR%/*}/Libraries/PI0.5" ]]; then
    OPENPI_ROOT="${PROJECTS_DIR%/*}/Libraries/PI0.5"
  fi
fi
DEST_DIR="${OPENPI_ROOT}/checkpoints/pi05_base"
if [[ "${1:-}" == "--print-destination" ]]; then
  printf '%s\n' "${DEST_DIR}"
  exit 0
fi
if [[ $# -gt 0 ]]; then
  echo "usage: $0 [--print-destination]" >&2
  exit 2
fi
MANIFEST_PATH="${DEST_DIR}/.gcs_objects.json"
LIST_URL="https://storage.googleapis.com/storage/v1/b/openpi-assets/o?prefix=checkpoints%2Fpi05_base%2F"
OBJECT_BASE_URL="https://storage.googleapis.com/openpi-assets"

command -v curl >/dev/null
command -v jq >/dev/null

mkdir -p "${DEST_DIR}"
curl -fsSL --silent --show-error --retry 5 --retry-all-errors \
  "${LIST_URL}" -o "${MANIFEST_PATH}.partial"
mv "${MANIFEST_PATH}.partial" "${MANIFEST_PATH}"

if [[ "$(jq -r '.nextPageToken // empty' "${MANIFEST_PATH}")" != "" ]]; then
  echo "STOP: the GCS object listing is paginated; update the downloader before continuing" >&2
  exit 1
fi

download_one() {
  local object_name="$1"
  local expected_size="$2"
  local relative_path="${object_name#checkpoints/pi05_base/}"
  local target_path="${DEST_DIR}/${relative_path}"
  local partial_path="${target_path}.partial"
  local actual_size

  if [[ "${relative_path}" == "${object_name}" || -z "${relative_path}" ]]; then
    echo "STOP: unexpected object path: ${object_name}" >&2
    return 1
  fi

  mkdir -p "$(dirname -- "${target_path}")"
  if [[ -f "${target_path}" ]]; then
    actual_size="$(stat -c '%s' "${target_path}")"
    if [[ "${actual_size}" == "${expected_size}" ]]; then
      echo "SKIP ${relative_path}"
      return 0
    fi
    echo "STOP: existing file has the wrong size: ${target_path}" >&2
    return 1
  fi

  echo "GET  ${relative_path} (${expected_size} bytes)"
  curl -fL --silent --show-error --retry 10 --retry-all-errors --continue-at - \
    "${OBJECT_BASE_URL}/${object_name}" -o "${partial_path}"

  actual_size="$(stat -c '%s' "${partial_path}")"
  if [[ "${actual_size}" != "${expected_size}" ]]; then
    echo "STOP: size mismatch for ${relative_path}: expected ${expected_size}, got ${actual_size}" >&2
    return 1
  fi
  mv "${partial_path}" "${target_path}"
}

export DEST_DIR OBJECT_BASE_URL
export -f download_one

jq -j '.items[] | .name, "\u0000", .size, "\u0000"' "${MANIFEST_PATH}" \
  | xargs -0 -n 2 -P 4 bash -c 'download_one "$1" "$2"' _

while IFS=$'\t' read -r object_name expected_size; do
  relative_path="${object_name#checkpoints/pi05_base/}"
  target_path="${DEST_DIR}/${relative_path}"
  actual_size="$(stat -c '%s' "${target_path}")"
  if [[ "${actual_size}" != "${expected_size}" ]]; then
    echo "STOP: final verification failed for ${relative_path}" >&2
    exit 1
  fi
done < <(jq -r '.items[] | [.name, .size] | @tsv' "${MANIFEST_PATH}")

echo "OK: pi05_base downloaded and size-verified at ${DEST_DIR}"

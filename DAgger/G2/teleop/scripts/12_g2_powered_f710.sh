#!/usr/bin/env bash
set -euo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/lib/paths.sh"
G2_APP_ROOT=/home/agi/app
DM05_DEPLOYMENT=/home/agi/xza/DM0.5/g2_sort/deployment
DM05_ROOT=/home/agi/xza/DM0.5

if [[ ! -d "${DM05_DEPLOYMENT}/g2_client" ]]; then
    echo "HOLD: missing validated DM0.5 g2_client at ${DM05_DEPLOYMENT}" >&2
    exit 2
fi

export PYTHONPATH="${G2_APP_ROOT}/gdk/lib:${G2TELEOP_ROOT}/vendor_ik:${G2TELEOP_ROOT}/src:${DM05_ROOT}:${DM05_DEPLOYMENT}${PYTHONPATH:+:${PYTHONPATH}}"
export LD_LIBRARY_PATH="${G2_APP_ROOT}/lib:${G2_APP_ROOT}/gdk/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
export APP_CONF_PATH="${G2_APP_ROOT}/gdk/config/app_conf.yml"
export AORTA_DISCOVERY_URI=http://10.42.1.101:2379
export AORTA_DISPATCHER_THREAD_NUM=6

G2_LOCATOR_IP=$(ip -o -4 addr list | awk '$4 ~ /^10\.42\.1\./ {sub(/\/.*/, "", $4); print $4; exit}')
if [[ -z "${G2_LOCATOR_IP}" ]]; then
    echo "HOLD: no existing 10.42.1.x G2 locator address" >&2
    exit 3
fi
export LOCATOR_IP="${G2_LOCATOR_IP}"

cd "${G2TELEOP_ROOT}"
echo "Running files-only preflight (no GDK, no robot command)..."
python3 scripts/12_g2_powered_f710.py \
    --preflight-files-only \
    --accept-dm05-hand-fit \
    "$@"

echo "This starts POWERED left/right arms and O10 hands only. Chassis is impossible here."
echo "A second person must hold the physical E-stop. Press Enter to continue, or Ctrl-C to abort."
read -r

mkdir -p "$G2TELEOP_LOG_DIR"
POWERED_LOG="${G2TELEOP_LOG_DIR}/powered_f710_console.log"
TELEMETRY_STAMP="$(date '+%Y%m%d_%H%M%S')"
TELEMETRY_MAIN="${DM05_ROOT}/logs/telemetry/powered/g2_powered_f710_${TELEMETRY_STAMP}.jsonl"
TELEMETRY_FEEDBACK="${DM05_ROOT}/logs/telemetry/feedback/g2_powered_f710_${TELEMETRY_STAMP}.jsonl"
echo "Powered console log: ${POWERED_LOG}"
echo "Structured telemetry: ${TELEMETRY_MAIN}"
set +e
# Ctrl-C is intended for the Python control owner.  Keep tee alive long enough
# for Python to publish both telemetry files and print the final status; if tee
# also exits on SIGINT, the next Python print sees a broken pipe and can abort
# the finally block before atomic .partial -> .jsonl publication.
python3 scripts/12_g2_powered_f710.py \
    --confirm-powered-arms-and-hands \
    --accept-dm05-hand-fit \
    --telemetry-jsonl "${TELEMETRY_MAIN}" \
    "$@" 2>&1 | (trap '' INT; tee -a "${POWERED_LOG}")
POWERED_STATUS="${PIPESTATUS[0]}"
set -e

upload_current_telemetry() {
    cd "${G2TELEOP_ROOT}"
    if [[ -s "${TELEMETRY_MAIN}" ]]; then
        if ! bash scripts/13_g2_push_telemetry.sh \
            --main "${TELEMETRY_MAIN}" \
            --feedback "${TELEMETRY_FEEDBACK}"; then
            echo "WARNING: telemetry upload failed; finalized G2 files are retained for retry." >&2
        fi
    else
        echo "WARNING: no finalized telemetry to upload; inspect any .partial file." >&2
    fi
}

upload_current_telemetry
exit "${POWERED_STATUS}"

#!/usr/bin/env bash
set -euo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/lib/paths.sh"
G2_APP_ROOT=/home/agi/app

export PYTHONPATH="${G2_APP_ROOT}/gdk/lib:${G2TELEOP_ROOT}/vendor_ik:${G2TELEOP_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
export LD_LIBRARY_PATH="${G2_APP_ROOT}/lib:${G2_APP_ROOT}/gdk/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
export APP_CONF_PATH="${G2_APP_ROOT}/gdk/config/app_conf.yml"
export AORTA_DISCOVERY_URI=http://10.42.1.101:2379
export AORTA_DISPATCHER_THREAD_NUM=6

G2_LOCATOR_IP=$(ip -o -4 addr list | awk '$4 ~ /^10\.42\.1\./ {sub(/\/.*/, "", $4); print $4; exit}')
if [[ -z "${G2_LOCATOR_IP}" ]]; then
    echo "HOLD: no existing 10.42.1.x G2 locator address" >&2
    exit 2
fi
export LOCATOR_IP="${G2_LOCATOR_IP}"

cd "${G2TELEOP_ROOT}"
mkdir -p "$G2TELEOP_LOG_DIR"
SHADOW_LOG="${G2TELEOP_LOG_DIR}/ruckig_otg_shadow_console.log"
TELEMETRY_STAMP="$(date '+%Y%m%d_%H%M%S')"
TELEMETRY_MAIN="/home/agi/xza/DM0.5/logs/telemetry/shadow/g2_shadow_f710_ruckig_${TELEMETRY_STAMP}.jsonl"
echo "Ruckig Shadow console log: ${SHADOW_LOG}"
echo "Ruckig Shadow telemetry: ${TELEMETRY_MAIN}"

set +e
python3 scripts/17_g2_ruckig_otg_shadow.py \
    --telemetry-jsonl "${TELEMETRY_MAIN}" \
    "$@" 2>&1 | (trap '' INT; tee -a "${SHADOW_LOG}")
SHADOW_STATUS="${PIPESTATUS[0]}"
set -e

if [[ -s "${TELEMETRY_MAIN}" ]]; then
    if ! bash scripts/13_g2_push_telemetry.sh \
        --category shadow \
        --main "${TELEMETRY_MAIN}"; then
        echo "WARNING: Shadow telemetry upload failed; finalized G2 file is retained." >&2
    fi
else
    echo "WARNING: no finalized Shadow telemetry to upload; inspect any .partial file." >&2
fi

exit "${SHADOW_STATUS}"

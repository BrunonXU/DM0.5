#!/usr/bin/env bash
set -euo pipefail

DAGGER_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
dagger_artifacts_root="${DAGGER_ARTIFACTS_ROOT:-$DAGGER_ROOT}"
if [[ -z "${DAGGER_ARTIFACTS_ROOT:-}" && -d "$DAGGER_ROOT/../shared/src/dagger_core" ]]; then
    dagger_artifacts_root="$DAGGER_ROOT/../artifacts"
fi
G2_APP_ROOT=/home/user/app
DM05_DEPLOYMENT=/home/user/xza/DM0.5/g2_sort/deployment
DM05_ROOT=/home/user/xza/DM0.5

if [[ ! -d "${DM05_DEPLOYMENT}/g2_client" ]]; then
    echo "HOLD: missing validated DM0.5 g2_client at ${DM05_DEPLOYMENT}" >&2
    exit 2
fi

export PYTHONPATH="${G2_APP_ROOT}/gdk/lib:${DAGGER_ROOT}/vendor_ik:${DAGGER_ROOT}/src:${DAGGER_ROOT}/../shared/src:${DM05_ROOT}:${DM05_DEPLOYMENT}${PYTHONPATH:+:${PYTHONPATH}}"
export LD_LIBRARY_PATH="${G2_APP_ROOT}/lib:${G2_APP_ROOT}/gdk/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
export APP_CONF_PATH="${G2_APP_ROOT}/gdk/config/app_conf.yml"
export AORTA_DISCOVERY_URI=http://192.0.2.9:2379
export AORTA_DISPATCHER_THREAD_NUM=6

G2_LOCATOR_IP=$(ip -o -4 addr list | awk '$4 ~ /^10\.42\.1\./ {sub(/\/.*/, "", $4); print $4; exit}')
if [[ -z "${G2_LOCATOR_IP}" ]]; then
    echo "HOLD: no existing 10.42.1.x G2 locator address" >&2
    exit 3
fi
export LOCATOR_IP="${G2_LOCATOR_IP}"

cd "${DAGGER_ROOT}"
echo "Running files-only preflight (no GDK, no robot command)..."
python3 scripts/40_g2_powered_hil.py \
    --preflight-files-only \
    --accept-dm05-hand-fit \
    "$@"

echo "POWERED HIL: connection waits without dispatch; first START begins real policy."
echo "Later START: POLICY -> human; release RB and finish hand intent, START -> HOLD -> fresh policy."
echo "Default duration 120 seconds after first START; --duration-seconds 0 runs until Ctrl-C. Same owner, native 17D policy / frozen F710 human semantics."
echo "A second person must hold the physical E-stop. Press Enter to continue, or Ctrl-C to abort."
read -r
for COUNTDOWN in 3 2 1; do echo "Powered starts in ${COUNTDOWN}"; sleep 1; done

mkdir -p "${dagger_artifacts_root}/logs"
POWERED_LOG="${dagger_artifacts_root}/logs/powered_hil_console.log"
TELEMETRY_STAMP="$(date '+%Y%m%d_%H%M%S')"
TELEMETRY_MAIN="${dagger_artifacts_root}/logs/telemetry/powered/g2_powered_hil_${TELEMETRY_STAMP}.jsonl"
TELEMETRY_FEEDBACK="${dagger_artifacts_root}/logs/telemetry/feedback/g2_powered_hil_${TELEMETRY_STAMP}.jsonl"
echo "Powered console log: ${POWERED_LOG}"
echo "Structured telemetry: ${TELEMETRY_MAIN}"
set +e
# Ctrl-C is intended for the Python control owner.  Keep tee alive long enough
# for Python to publish both telemetry files and print the final status; if tee
# also exits on SIGINT, the next Python print sees a broken pipe and can abort
# the finally block before atomic .partial -> .jsonl publication.
python3 scripts/40_g2_powered_hil.py \
    --confirm-powered-arms-and-hands \
    --accept-dm05-hand-fit \
    --telemetry-jsonl "${TELEMETRY_MAIN}" \
    "$@" 2>&1 | (trap '' INT; tee -a "${POWERED_LOG}")
POWERED_STATUS="${PIPESTATUS[0]}"
set -e

# Logs stay under DAgger; no legacy G2Teleop uploader.
exit "${POWERED_STATUS}"

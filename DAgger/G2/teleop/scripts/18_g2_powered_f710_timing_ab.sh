#!/usr/bin/env bash
set -euo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/lib/paths.sh"

if [[ $# -lt 1 ]]; then
    echo "Usage: bash scripts/18_g2_powered_f710_timing_ab.sh direct|otg [extra args]" >&2
    exit 2
fi

VARIANT="$1"
shift

case "${VARIANT}" in
    direct)
        VARIANT_ARGS=()
        ;;
    otg)
        VARIANT_ARGS=(--enable-ruckig-otg)
        ;;
    *)
        echo "HOLD: variant must be exactly direct or otg" >&2
        exit 2
        ;;
esac

echo "F710 TIMING A/B variant=${VARIANT}"
echo "Normal collision-protected arms Servo remains enabled; low-latency is forbidden."
echo "T5 feedback is disabled and full pipeline telemetry is reduced to 20 Hz."
echo "Every 100 Hz GDK dispatch time and 14D arm command remain recorded."

cd "${G2TELEOP_ROOT}"
exec bash scripts/12_g2_powered_f710.sh \
    --diagnostic-profile timing-ab \
    "${VARIANT_ARGS[@]}" \
    "$@"

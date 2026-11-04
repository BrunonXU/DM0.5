#!/usr/bin/env bash
# F1 onsite only. Separate immutable model bindings; shared robot control lock.
set -eo pipefail
step=${1:-}
case "$step" in
    2000|5000|14000) shift ;;
    -h|--help) echo 'Usage: bash start_f1_toys_compare.sh {2000|5000|14000} [--continuous | --duration-seconds N] [--shadow]'; exit 0 ;;
    *) echo 'Choose checkpoint 2000, 5000 or 14000.' >&2; exit 2 ;;
esac
mode=sync
execute=(--execute)
timing=()
while (($#)); do
    case "$1" in
        --shadow) mode=sync-shadow; execute=(); shift ;;
        --duration-seconds)
            [[ $# -ge 2 ]] || { echo '--duration-seconds requires a value' >&2; exit 2; }
            timing+=("$1" "$2"); shift 2 ;;
        --duration-seconds=*|--continuous) timing+=("$1"); shift ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done
set -a
source /home/user/Bruno/DAgger-runtime/f1-v18-site.env
set +a
source /opt/ros/jazzy/setup.bash
source /home/user/workspace/f1/install/setup.bash
source /home/user/workspace/agv/install/setup.bash
unset F1_DAGGER_ENV F1_HIL_SESSION_CONFIG
export F1_DAGGER_RUNTIME=/home/user/Bruno/DAgger-runtime/f1-toys-20261016/compare-${step}-runtime
export PYTHONPATH=/home/user/Bruno/DAgger-runtime/f1-toys-20261016/python${PYTHONPATH:+:$PYTHONPATH}
mkdir -p "$F1_DAGGER_RUNTIME"
set -u
echo "V1 comparison checkpoint=$step; local Enter starts motion; Ctrl+C stops."
source "${F1_CONSOLE_HELPER:-/home/user/Bruno/DAgger-runtime/console-20261022-r2/inference_console.sh}"
f1_console_run "$F1_DAGGER_RUNTIME/onsite-policy-console.log" bash /home/user/Bruno/DAgger/releases/20261016-f1-toys-eef-v5-telemetry-shadow-off-20261022/DAgger/scripts/50_f1_hil.sh \
    "$mode" "${execute[@]}" "${timing[@]}" --purpose policy_hil --video off --recording-mode hil-record \
    --config "/home/user/Bruno/DAgger-runtime/f1-toys-20261016/config-compare-${step}.json" \
    --runtime "$F1_DAGGER_RUNTIME" --data-root /home/user/Bruno/data/hil_data \
    --task 'Use the right gripper to pick up the wooden block and insert it into the matching opening of the container.'

#!/usr/bin/env bash
# Independent Full 250 entry. Only local Enter authorizes robot motion.
set -eo pipefail
mode=sync
execute=(--execute)
timing=()
while (($#)); do
    case "$1" in
        --shadow) mode=sync-shadow; execute=(); shift ;;
        --duration-seconds)
            [[ $# -ge 2 ]] || { echo 'Missing duration' >&2; exit 2; }
            timing+=("$1" "$2"); shift 2 ;;
        --duration-seconds=*|--continuous) timing+=("$1"); shift ;;
        -h|--help)
            echo 'Usage: bash start_f1_toys_full_250.sh [--duration-seconds 20 | --continuous] [--shadow]'
            echo 'Enter starts; Ctrl+C stops. Learned return is part of the policy; exit does not issue Home.'
            exit 0 ;;
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
full_root=/home/user/Bruno/DAgger-runtime/f1-toys-20261016/full-rtc/250
export F1_DAGGER_RUNTIME="$full_root/runtime"
export PYTHONPATH=/home/user/Bruno/DAgger-runtime/f1-toys-20261016/python${PYTHONPATH:+:$PYTHONPATH}
mkdir -p "$F1_DAGGER_RUNTIME"
set -u
python3 "$full_root/ensure_full_rtc_tunnel.py"
source "${F1_CONSOLE_HELPER:-/home/user/Bruno/DAgger-runtime/console-20261022-r2/inference_console.sh}"
f1_console_run "$F1_DAGGER_RUNTIME/onsite-policy-console.log" bash /home/user/Bruno/DAgger/releases/20261020-f1-toys-rtc-v5-fastpixels-shadow-off-20261022/DAgger/scripts/50_f1_hil.sh \
    "$mode" --rtc --rtc-delay-steps 20 --rtc-min-steps 20 "${execute[@]}" "${timing[@]}" \
    --purpose policy_hil --video off --recording-mode hil-record \
    --config "$full_root/config.json" --runtime "$F1_DAGGER_RUNTIME" \
    --data-root /home/user/Bruno/data/hil_data \
    --task 'Use the right gripper to pick up the wooden block, insert it into the matching opening of the container, release it, and return the right arm to the starting pose with the gripper open.'

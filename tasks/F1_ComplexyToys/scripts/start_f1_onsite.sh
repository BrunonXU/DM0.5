#!/usr/bin/env bash
# Install on F1 as /home/user/Bruno/DAgger/start_f1_toys_policy.sh.
# Onsite standalone sync. Enter authorizes; no gamepad or Windows sender.
set -eo pipefail
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
        -h|--help)
            echo 'Usage: bash start_f1_toys_policy.sh [--duration-seconds 20 | --continuous] [--shadow]'
            echo 'Default: 60 seconds wall time after local Enter; Ctrl+C stops; no automatic reset.'
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
export F1_DAGGER_RUNTIME=/home/user/Bruno/DAgger-runtime/f1-toys-20261016/standalone-runtime
export PYTHONPATH=/home/user/Bruno/DAgger-runtime/f1-toys-20261016/python${PYTHONPATH:+:$PYTHONPATH}
mkdir -p "$F1_DAGGER_RUNTIME"
set -u
set -o pipefail
source "${F1_CONSOLE_HELPER:-/home/user/Bruno/DAgger-runtime/console-20261022-r2/inference_console.sh}"
f1_console_run "$F1_DAGGER_RUNTIME/onsite-policy-console.log" bash /home/user/Bruno/DAgger/releases/20261016-f1-toys-eef-v5-telemetry-shadow-off-20261022/DAgger/scripts/50_f1_hil.sh \
    "$mode" "${execute[@]}" "${timing[@]}" --purpose policy_hil --video off --recording-mode hil-record \
    --config /home/user/Bruno/DAgger-runtime/f1-toys-20261016/config-standalone.json \
    --runtime "$F1_DAGGER_RUNTIME" --data-root /home/user/Bruno/data/hil_data \
    --task 'Use the right gripper to pick up the wooden block and insert it into the matching opening of the container.'

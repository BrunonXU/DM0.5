#!/usr/bin/env bash
# Independent 50K recovery experiment. Powered execution requires onsite Enter.
set -eo pipefail
candidate=/home/user/Bruno/DAgger-runtime/ik-recovery-20261021-r3
mode=sync
execute=(--execute)
timing=()
while (($#)); do
    case "$1" in
        --shadow) mode=sync-shadow; execute=(); shift ;;
        --duration-seconds) [[ $# -ge 2 ]] || exit 2; timing+=("$1" "$2"); shift 2 ;;
        --duration-seconds=*|--continuous) timing+=("$1"); shift ;;
        -h|--help)
            echo 'Usage: bash start_f1_ik_recovery.sh [--duration-seconds 60 | --continuous] [--shadow]'
            echo 'Independent original-limit 50K recovery; onsite Enter starts, Ctrl+C stops; no automatic reset.'
            exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done
if [[ $mode == sync && ! -t 0 ]]; then
    echo 'Powered recovery requires a local interactive terminal and Enter.' >&2
    exit 2
fi
set -a
source /home/user/Bruno/DAgger-runtime/f1-v18-site.env
set +a
source /opt/ros/jazzy/setup.bash
source /home/user/workspace/f1/install/setup.bash
source /home/user/workspace/agv/install/setup.bash
unset F1_DAGGER_ENV F1_HIL_SESSION_CONFIG
export F1_DAGGER_RUNTIME="$candidate/runtime"
export PYTHONPATH="$candidate/DAgger/src:$F1_INFRA_ROOT:/home/user/Bruno/DAgger-runtime/f1-toys-20261016/python${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p "$F1_DAGGER_RUNTIME"
set -u
python3 "$candidate/ensure_ik_recovery_tunnel.py"
source "${F1_CONSOLE_HELPER:-/home/user/Bruno/DAgger-runtime/console-20261022-r2/inference_console.sh}"
f1_console_run "$F1_DAGGER_RUNTIME/onsite-policy-console.log" "${F1_DAGGER_PYTHON:-python3}" "$candidate/DAgger/scripts/run_f1_ik_tracking_recovery.py" \
    "$mode" --rtc --rtc-delay-steps 20 --rtc-min-steps 20 "${execute[@]}" "${timing[@]}" \
    --purpose policy_hil --video off --recording-mode hil-record \
    --config "$candidate/config.json" --runtime "$F1_DAGGER_RUNTIME" \
    --data-root /home/user/Bruno/data/hil_data \
    --task 'Use the right gripper to pick up the wooden block and insert it into the matching opening of the container.'

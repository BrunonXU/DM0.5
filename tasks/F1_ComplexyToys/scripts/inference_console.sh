#!/usr/bin/env bash
# Source before f1_console_run. Never redirect or synthesize operator stdin.
F1_CONSOLE_MODULE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
f1_console_run() {
    local raw_log=$1
    shift
    local -a result
    if [[ ${F1_CONSOLE_VERBOSE:-0} == 1 ]]; then
        if "$@" 2>&1 | tee -i -a "$raw_log"; then return 0; else return "$?"; fi
    fi
    # tee retains every original byte before the display-only transformation.
    if "$@" 2>&1 | tee -i -a "$raw_log" | python3 -u "$F1_CONSOLE_MODULE_DIR/inference_console.py" --raw-log "$raw_log" -- "$@"; then
        return 0
    else
        result=("${PIPESTATUS[@]}")
        # Keep the command failure; otherwise expose logging/display failures.
        if [[ ${result[0]} != 0 ]]; then return "${result[0]}"; fi
        if [[ ${result[1]} != 0 ]]; then return "${result[1]}"; fi
        return "${result[2]}"
    fi
}

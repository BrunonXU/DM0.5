#!/usr/bin/env bash
# Explicit session launcher. Defaults to shadow; never infers powered permission.
set -euo pipefail
f1_session_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${F1_DAGGER_ENV:-}" ]]; then
  set -a
  source "${F1_DAGGER_ENV}"
  set +a
fi
: "${F1_HIL_SESSION_CONFIG:?Set F1_HIL_SESSION_CONFIG to 4242 runtime/hil-session.json}"
f1_execution="${1:---shadow}"
if [[ $# -gt 0 ]]; then shift; fi
case "$f1_execution" in --shadow|--powered) ;; *) echo 'Use --shadow or --powered, then regular F1 CLI options' >&2; exit 2 ;; esac
export PYTHONPATH="${f1_session_root}/src:${f1_session_root}/../shared/src${PYTHONPATH:+:${PYTHONPATH}}"
f1_purpose="$("${F1_DAGGER_PYTHON:-python3}" -c 'import sys; from f1dagger.session import read_session; print(read_session(sys.argv[1])["purpose"])' "$F1_HIL_SESSION_CONFIG")"
f1_mode=shadow
f1_permission=()
if [[ "$f1_execution" == --powered ]]; then
  if [[ "$f1_purpose" == input_check ]]; then echo 'Input check requires --shadow' >&2; exit 2; fi
  f1_mode=teleop
  f1_permission=(--execute)
  if [[ "$f1_purpose" == policy_hil ]]; then f1_mode=run; fi
elif [[ "$f1_purpose" == policy_hil ]]; then
  f1_mode=policy-shadow
fi
exec bash "$f1_session_root/scripts/50_f1_hil.sh" "$f1_mode" --session-config "$F1_HIL_SESSION_CONFIG" "${f1_permission[@]}" "$@"

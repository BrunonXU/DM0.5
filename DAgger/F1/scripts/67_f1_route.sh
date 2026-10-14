#!/usr/bin/env bash
# Backend-owned route settings; calling this script is an explicit terminal action.
set -euo pipefail
f1_route_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${F1_DAGGER_ENV:-}" ]]; then
  set -a; source "$F1_DAGGER_ENV"; set +a
fi
case "${1:-}" in
 play|demonstration|policy) f1_route="$1"; shift ;;
 *) echo 'Usage: 67_f1_route.sh play|demonstration|policy [F1 CLI options]' >&2; exit 2 ;;
esac
: "${F1_HIL_ROUTE_DIR:?Set backend route configuration directory}"
export F1_HIL_SESSION_CONFIG="${F1_HIL_ROUTE_DIR}/${f1_route}.json"
f1_execution="${F1_HIL_ROUTE_EXECUTION:---shadow}"
# The route owns its session file. Do not let the legacy environment reload replace it.
unset F1_DAGGER_ENV
exec bash "$f1_route_root/scripts/66_f1_session.sh" "$f1_execution" "$@"

#!/usr/bin/env bash
# Push finalized F710 telemetry to the 6000D dashboard inbox.
set -Eeuo pipefail

SERVER_HOST="${G2_TELEMETRY_SERVER_HOST:-192.0.2.1}"
SERVER_USER="${G2_TELEMETRY_SERVER_USER:-user}"
RECEIVER="${G2_TELEMETRY_RECEIVER:-/home/user/projects/g2_robot/scripts/receive_g2_telemetry.sh}"
main=""
feedback=""
category="powered"
control_path="/tmp/g2telemetry-ssh-$$"

while (( $# > 0 )); do
  case "$1" in
    --main) (( $# >= 2 )) || { echo "STOP: --main缺少路径" >&2; exit 2; }; main="$2"; shift 2 ;;
    --feedback) (( $# >= 2 )) || { echo "STOP: --feedback缺少路径" >&2; exit 2; }; feedback="$2"; shift 2 ;;
    --category) (( $# >= 2 )) || { echo "STOP: --category缺少值" >&2; exit 2; }; category="$2"; shift 2 ;;
    -h|--help) echo "usage: $0 --main MAIN.jsonl [--category powered|shadow] [--feedback FEEDBACK.jsonl]"; exit 0 ;;
    *) echo "STOP: 未知参数：$1" >&2; exit 2 ;;
  esac
done

[[ "$category" == "powered" || "$category" == "shadow" ]] || {
  echo "STOP: --category只允许powered或shadow" >&2
  exit 2
}
[[ "$category" == "powered" || -z "$feedback" ]] || {
  echo "STOP: shadow telemetry不接受feedback sidecar" >&2
  exit 2
}

[[ -n "$main" && -s "$main" && "$main" == *.jsonl ]] || {
  echo "STOP: 主telemetry必须是已封口的非空.jsonl" >&2
  exit 2
}
ssh_common=(
  -o ConnectTimeout=8
  -o StrictHostKeyChecking=yes
  -o PubkeyAuthentication=no
  -o PreferredAuthentications=keyboard-interactive,password
  -o ControlPath="$control_path"
)

close_master() {
  [[ -S "$control_path" ]] || return 0
  ssh "${ssh_common[@]}" -O exit "$SERVER_USER@$SERVER_HOST" \
    >/dev/null 2>&1 || true
}
trap close_master EXIT

# Authenticate once on the controlling terminal. Both JSONL streams then use
# this short-lived multiplexed connection; no password or socket is retained.
ssh "${ssh_common[@]}" \
  -o ControlMaster=yes \
  -o ControlPersist=60 \
  -Nf \
  "$SERVER_USER@$SERVER_HOST"

push_one() {
  local category="$1" source="$2" filename
  [[ -s "$source" && "$source" == *.jsonl ]] || return 0
  filename="$(basename -- "$source")"
  ssh "${ssh_common[@]}" \
    "$SERVER_USER@$SERVER_HOST" \
    "$RECEIVER telemetry-put $category $filename" < "$source"
}

push_one "$category" "$main"
if [[ -n "$feedback" && -f "$feedback" ]]; then
  push_one feedback "$feedback"
fi
echo "F710 ${category} telemetry uploaded; G2 originals retained."

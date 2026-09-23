#!/usr/bin/env bash
# Receiver for finalized G2 telemetry. It supports direct password-authenticated
# invocation and the former SSH forced-command form.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
if (( $# > 0 )); then
  (( $# == 3 )) || {
    echo "DENY: expected telemetry-put <powered|shadow|feedback> <filename>" >&2
    exit 64
  }
  verb="$1"
  category="$2"
  filename="$3"
  extra=""
else
  original="${SSH_ORIGINAL_COMMAND:-}"
  read -r verb category filename extra <<<"$original"
fi

[[ "$verb" == "telemetry-put" && -z "${extra:-}" ]] || {
  echo "DENY: only telemetry-put <powered|shadow|feedback> <filename> is allowed" >&2
  exit 64
}
[[ "$category" == "powered" || "$category" == "shadow" || "$category" == "feedback" ]] || {
  echo "DENY: invalid telemetry category" >&2
  exit 64
}
if [[ "$category" == "shadow" ]]; then
  filename_pattern='^g2_shadow_f710_ruckig_[0-9]{8}_[0-9]{6}\.jsonl$'
else
  filename_pattern='^g2_powered_f710_[0-9]{8}_[0-9]{6}\.jsonl$'
fi
[[ "$filename" =~ $filename_pattern ]] || {
  echo "DENY: invalid F710 telemetry filename" >&2
  exit 64
}

destination_dir="$REPO_ROOT/logs/telemetry/$category"
destination="$destination_dir/$filename"
mkdir -p "$destination_dir"

if [[ -e "$destination" ]]; then
  # Idempotent retry: consume stdin so the sender exits cleanly, then preserve
  # the first immutable copy.
  dd of=/dev/null bs=1M status=none
  echo "EXISTS: logs/telemetry/$category/$filename"
  exit 0
fi

temporary="$(mktemp "$destination_dir/.g2-f710-upload.XXXXXX")"
trap 'rm -f -- "$temporary"' EXIT
dd of="$temporary" bs=1M status=none
[[ -s "$temporary" ]] || {
  echo "DENY: empty telemetry upload" >&2
  exit 65
}

# The G2 producer only publishes finalized JSONL.  Require a newline-terminated
# stream so a truncated network transfer never becomes discoverable.
[[ "$(tail -c 1 -- "$temporary" | od -An -t u1 | tr -d ' ')" == "10" ]] || {
  echo "DENY: telemetry stream is not newline-terminated" >&2
  exit 65
}

mv -n -- "$temporary" "$destination"
trap - EXIT
echo "STORED: logs/telemetry/$category/$filename"

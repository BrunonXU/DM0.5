#!/usr/bin/env bash
# Source paths only: no environment installation, network, or hardware startup.
G2TELEOP_ROOT="${G2TELEOP_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)}"
if [[ -z "${G2TELEOP_LOG_DIR:-}" ]]; then
  if [[ "$G2TELEOP_ROOT" == */G2/teleop ]]; then
    G2TELEOP_LOG_DIR="${G2TELEOP_ROOT%/G2/teleop}/artifacts/logs/g2_teleop"
  else
    G2TELEOP_LOG_DIR="$G2TELEOP_ROOT/logs"
  fi
fi

#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ -f "${F1_INSTALLATION_ENV:-$HERE/configs/installation.env}" ]]; then
  source "${F1_INSTALLATION_ENV:-$HERE/configs/installation.env}"
fi
cat "${F1_CONSOLE_RUNTIME:-${F1_INFRA_HOME:-$HOME/Bruno/F1-infra}/runtime}/access.token"

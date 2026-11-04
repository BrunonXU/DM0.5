#!/usr/bin/env bash
set -euo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/lib/paths.sh"
G2_APP_ROOT=/home/user/app

export PYTHONPATH="${G2_APP_ROOT}/gdk/lib:${G2TELEOP_ROOT}/vendor_ik:${G2TELEOP_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
export LD_LIBRARY_PATH="${G2_APP_ROOT}/lib:${G2_APP_ROOT}/gdk/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
export APP_CONF_PATH="${G2_APP_ROOT}/gdk/config/app_conf.yml"
export AORTA_DISCOVERY_URI=http://192.0.2.9:2379
export AORTA_DISPATCHER_THREAD_NUM=6

G2_LOCATOR_IP=$(ip -o -4 addr list | awk '$4 ~ /^10\.42\.1\./ {sub(/\/.*/, "", $4); print $4; exit}')
if [[ -z "${G2_LOCATOR_IP}" ]]; then
    echo "HOLD: no existing 10.42.1.x G2 locator address; refusing to initialize GDK" >&2
    exit 2
fi
export LOCATOR_IP="${G2_LOCATOR_IP}"

cd "${G2TELEOP_ROOT}"
exec python3 scripts/10_g2_live_ik_shadow.py "$@"

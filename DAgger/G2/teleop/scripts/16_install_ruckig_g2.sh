#!/usr/bin/env bash
set -euo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/lib/paths.sh"
RUCKIG_VERSION=0.19.4
RUCKIG_INDEX_URL="${RUCKIG_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
TARGET="${G2TELEOP_ROOT}/vendor_ik"

if [[ "$(uname -m)" != "aarch64" ]]; then
    echo "HOLD: this installer is only for the G2 aarch64 computer" >&2
    exit 2
fi
if [[ ! -d "${G2TELEOP_ROOT}" ]]; then
    echo "HOLD: missing ${G2TELEOP_ROOT}" >&2
    exit 3
fi

if PYTHONPATH="${TARGET}${PYTHONPATH:+:${PYTHONPATH}}" python3 -c \
    "import ruckig,sys; sys.exit(0 if ruckig.__version__ == '${RUCKIG_VERSION}' else 1)" \
    2>/dev/null; then
    echo "Ruckig ${RUCKIG_VERSION} already installed in ${TARGET}"
    exit 0
fi

command -v c++ >/dev/null || {
    echo "HOLD: C++ compiler missing; Ruckig has no aarch64/Python 3.12 wheel" >&2
    exit 4
}
command -v cmake >/dev/null || {
    echo "HOLD: cmake missing; Ruckig must be built from source on G2" >&2
    exit 5
}

echo "Installing official Ruckig ${RUCKIG_VERSION} into ${TARGET}"
echo "Index: ${RUCKIG_INDEX_URL}; source build expected on aarch64/Python 3.12"
mkdir -p "${TARGET}"
CMAKE_BUILD_PARALLEL_LEVEL=2 python3 -m pip install \
    --target "${TARGET}" \
    --no-deps \
    --no-cache-dir \
    --index-url "${RUCKIG_INDEX_URL}" \
    "ruckig==${RUCKIG_VERSION}"

PYTHONPATH="${TARGET}${PYTHONPATH:+:${PYTHONPATH}}" python3 - <<'PY'
import ruckig

assert ruckig.__version__ == "0.19.4", ruckig.__version__
otg = ruckig.Ruckig(1, 0.01)
inp = ruckig.InputParameter(1)
out = ruckig.OutputParameter(1)
inp.control_interface = ruckig.ControlInterface.Velocity
inp.current_position = [0.0]
inp.current_velocity = [0.0]
inp.current_acceleration = [0.0]
inp.target_velocity = [0.1]
inp.target_acceleration = [0.0]
inp.max_velocity = [0.35]
inp.max_acceleration = [2.0]
inp.max_jerk = [20.0]
result = otg.update(inp, out)
assert result in (ruckig.Result.Working, ruckig.Result.Finished), result
assert 0.0 < out.new_velocity[0] <= 0.1
print(f"Ruckig {ruckig.__version__} G2 smoke PASS: dq1={out.new_velocity[0]:.6f}")
PY

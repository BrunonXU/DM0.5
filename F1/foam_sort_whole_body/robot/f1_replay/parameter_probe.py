"""Isolated Fast DDS parameter reader; never creates motion publishers."""
from __future__ import annotations

import json
import math
import os
import sys

from .ros_backend import RosBackend


class ReadOnlyParameterBackend(RosBackend):
    def enable_commands(self):
        raise RuntimeError("只读参数子进程禁止创建控制发布者")

    def publish(self, action):
        raise RuntimeError("只读参数子进程禁止下发动作")

    def stop_base(self):
        raise RuntimeError("只读参数子进程禁止下发底盘指令")

    def hold_arms(self):
        raise RuntimeError("只读参数子进程禁止下发双臂指令")


def probe(request):
    profile = request.get("profile")
    timeout = request.get("timeout_sec")
    if (os.environ.get("RMW_IMPLEMENTATION") != "rmw_fastrtps_cpp"
            or not isinstance(profile, dict)
            or profile.get("controller_parameter_rmw") != "rmw_fastrtps_cpp"):
        raise ValueError("只读参数子进程要求显式匹配的rmw_fastrtps_cpp环境和配置")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("参数读取timeout_sec必须为有限正数")
    backend = ReadOnlyParameterBackend(profile)
    try:
        backend.open()
        return backend.read_controller_parameters(timeout_sec=timeout)
    finally:
        backend.close()


def main():
    try:
        if len(sys.argv) != 1:
            raise ValueError("只读参数子进程不接受命令行控制参数")
        result = probe(json.load(sys.stdin))
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                          "diagnostics": getattr(exc, "diagnostics", None)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

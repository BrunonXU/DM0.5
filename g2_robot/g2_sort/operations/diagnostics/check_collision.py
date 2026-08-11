#!/usr/bin/env python3
"""G2碰撞与运动状态只读检查。

用途：快速确认DDS返回的MotionControlStatus；输入：当前GDK环境与机器人状态；
输出：连续状态打印或非零异常；方法：只调用状态接口，绝不构造Servo命令。
"""

import time


def main():
    import agibot_gdk as gdk

    initialized = False
    try:
        result = gdk.gdk_init()
        if result != gdk.GDKRes.kSuccess:
            raise RuntimeError(f"gdk_init failed: {result}")

        initialized = True
        robot = gdk.Robot()
        time.sleep(2)

        status = robot.get_motion_control_status()
        pairs = list(zip(
            list(getattr(status, "collision_pairs_1", [])),
            list(getattr(status, "collision_pairs_2", [])),
        ))

        print("===== G2 STATUS =====")
        print("mode=", getattr(status, "mode", None))
        print("error_code=", getattr(status, "error_code", None))
        print("error_msg=", repr(getattr(status, "error_msg", None)))
        print("collision_pairs=", pairs)
        print("READ ONLY: no command was sent")

        return 0 if int(getattr(status, "error_code", -1)) == 0 else 2
    finally:
        if initialized:
            gdk.gdk_release()


if __name__ == "__main__":
    raise SystemExit(main())

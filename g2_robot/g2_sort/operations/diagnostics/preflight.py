"""G2只读上机预检。

用途：一次检查GDK、运动状态、严格34D state和三路RGB相机。
输入：输出目录及当前GDK/DDS数据。
输出：一个JSON报告和头、左腕、右腕三张JPEG。
方法：初始化GDK后只调用状态/相机读取接口，不构造控制器也不发送动作。
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

from g2_sort.deployment.g2_client.cameras import G2CameraRig
from g2_sort.deployment.g2_client.joint_layout import POLICY_JOINT_NAMES
from g2_sort.deployment.g2_client.robot_state import StrictRobotStateReader


def _jsonable(value: object) -> object:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return str(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--stabilization-seconds", type=float, default=10.0)
    parser.add_argument("--camera-timeout-ms", type=float, default=500.0)
    parser.add_argument("--camera-stabilization-seconds", type=float, default=3.0)
    parser.add_argument("--camera-retries", type=int, default=5)
    parser.add_argument("--camera-retry-seconds", type=float, default=1.0)
    args = parser.parse_args()
    if args.camera_stabilization_seconds < 0:
        parser.error("--camera-stabilization-seconds cannot be negative")
    if args.camera_retries < 1:
        parser.error("--camera-retries must be positive")
    if args.camera_retry_seconds < 0:
        parser.error("--camera-retry-seconds cannot be negative")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    import agibot_gdk as gdk
    from PIL import Image

    initialized = False
    cameras = None
    try:
        print("【预检 1/5】正在初始化GDK……", flush=True)
        result = gdk.gdk_init()
        if result != gdk.GDKRes.kSuccess:
            raise RuntimeError(f"gdk_init failed: {result}")
        initialized = True
        print(
            f"【预检 2/5】等待DDS稳定（约{args.stabilization_seconds:.0f}秒）……",
            flush=True,
        )
        robot = gdk.Robot()
        time.sleep(args.stabilization_seconds)
        whole = robot.get_whole_body_status()
        motion = robot.get_motion_control_status()
        print(
            "【预检 3/5】运动状态："
            f"mode={int(getattr(motion, 'mode', -1))} "
            f"error_code={int(getattr(motion, 'error_code', -1))}",
            flush=True,
        )
        state = StrictRobotStateReader(robot).read()
        print(
            "【预检 4/5】34D关节状态读取成功："
            f"shape={tuple(state.shape)} finite={bool(np.all(np.isfinite(state)))}",
            flush=True,
        )
        cameras = G2CameraRig(
            timeout_ms=args.camera_timeout_ms,
            stabilization_seconds=args.camera_stabilization_seconds,
            gdk_module=gdk,
        )
        print("【预检 5/5】正在采集头部、左腕、右腕三路相机……", flush=True)
        camera_names = (
            ("head", "头部相机"),
            ("left_wrist", "左腕相机"),
            ("right_wrist", "右腕相机"),
        )
        captured_images = []
        camera_errors = []
        for name, display_name in camera_names:
            image = None
            last_camera_error = None
            for attempt in range(1, args.camera_retries + 1):
                try:
                    image = cameras.capture_channel(name)
                    break
                except Exception as exc:
                    last_camera_error = exc
                    if attempt < args.camera_retries:
                        print(
                            f"  - {display_name}第{attempt}次无帧，"
                            f"{args.camera_retry_seconds:.0f}秒后重试……",
                            flush=True,
                        )
                        time.sleep(args.camera_retry_seconds)
            if image is None:
                camera_errors.append(f"{display_name}: {last_camera_error}")
                print(f"  - 【失败】{display_name}连续取帧失败", flush=True)
            else:
                captured_images.append((name, display_name, image))
                print(
                    f"  - 【成功】{display_name}: shape={tuple(image.shape)} "
                    f"亮度范围={int(np.min(image))}～{int(np.max(image))}",
                    flush=True,
                )
        if camera_errors:
            raise RuntimeError(
                "相机预检失败：" + "；".join(camera_errors)
            )
        image_records = []
        for name, display_name, image in captured_images:
            path = args.output_dir / f"{name}.jpg"
            Image.fromarray(image, mode="RGB").save(path, quality=95)
            image_records.append(
                {
                    "name": name,
                    "path": str(path),
                    "shape": list(image.shape),
                    "dtype": str(image.dtype),
                    "min": int(np.min(image)),
                    "max": int(np.max(image)),
                }
            )
        report = {
            "generated_at_unix": time.time(),
            "read_only": True,
            "motion": {
                "mode": int(getattr(motion, "mode", -1)),
                "error_code": int(getattr(motion, "error_code", -1)),
                "error_msg": str(getattr(motion, "error_msg", "")),
                "collision_pairs": list(
                    zip(
                        list(getattr(motion, "collision_pairs_1", []) or []),
                        list(getattr(motion, "collision_pairs_2", []) or []),
                    )
                ),
            },
            "whole_body": _jsonable(whole),
            "state": {
                "shape": list(state.shape),
                "finite": bool(np.all(np.isfinite(state))),
                "names": list(POLICY_JOINT_NAMES),
                "values": state.tolist(),
            },
            "images": image_records,
        }
        report_path = args.output_dir / "preflight.json"
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        if report["motion"]["error_code"] != 0:
            raise RuntimeError(
                f"motion error_code={report['motion']['error_code']}"
            )
        if report["motion"]["mode"] != 5:
            raise RuntimeError(
                f"G2 must already be in servo mode 5, got {report['motion']['mode']}"
            )
        print("【预检通过】GDK、34D状态和三路相机均正常；全程未发送动作。")
        print(f"【报告位置】{report_path}")
    finally:
        if cameras is not None:
            cameras.close()
        if initialized:
            gdk.gdk_release()


if __name__ == "__main__":
    main()

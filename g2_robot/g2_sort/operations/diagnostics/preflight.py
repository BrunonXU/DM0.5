"""G2只读上机预检。

用途：一次检查GDK、运动状态、严格34D state和三路RGB相机。
输入：输出目录及当前GDK/DDS数据。
输出：一个JSON报告和头、左腕、右腕三张JPEG。
方法：初始化GDK后只调用状态/相机读取接口，不构造控制器也不发送动作。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from io import BytesIO
from pathlib import Path

import numpy as np

from g2_sort.deployment.g2_client.cameras import G2CameraRig
from g2_sort.deployment.g2_client.dm05_backend import encode_rgb_jpeg
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
                    image, native_frame = cameras.capture_transport_pair(name)
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
                captured_images.append(
                    (name, display_name, image, native_frame)
                )
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
        native_dir = args.output_dir / "native"
        for name, display_name, image, native_frame in captured_images:
            path = args.output_dir / f"{name}.jpg"
            # Use the production HTTP backend's encoder and persist those exact
            # bytes.  The archive can therefore be replayed as the actual wire
            # payload instead of being an independently re-saved screenshot.
            jpeg_payload = encode_rgb_jpeg(image)
            path.write_bytes(jpeg_payload)
            with Image.open(BytesIO(jpeg_payload)) as opened:
                wire_rgb = np.asarray(opened.convert("RGB"), dtype=np.uint8)
            wire_delta = np.abs(
                wire_rgb.astype(np.int16) - image.astype(np.int16)
            )
            record = {
                    "name": name,
                    "path": str(path),
                    "wire_filename": f"{name}.jpg",
                    "wire_media_type": "image/jpeg",
                    "wire_encoder": "g2_client.encode_rgb_jpeg_quality95",
                    "wire_bytes": len(jpeg_payload),
                    "wire_sha256": hashlib.sha256(jpeg_payload).hexdigest(),
                    "shape": list(image.shape),
                    "dtype": str(image.dtype),
                    "min": int(np.min(image)),
                    "max": int(np.max(image)),
                    "wire_decode_mae_uint8": float(wire_delta.mean()),
                    "wire_decode_max_abs_uint8": int(wire_delta.max()),
                }
            if native_frame is not None:
                native_dir.mkdir(parents=True, exist_ok=True)
                native_suffix = (
                    ".jpg"
                    if native_frame.media_type == "image/jpeg"
                    else ".png"
                )
                native_relative = Path("native") / f"{name}{native_suffix}"
                native_path = args.output_dir / native_relative
                native_path.write_bytes(native_frame.data)
                record.update(
                    {
                        "native_filename": native_relative.as_posix(),
                        "native_media_type": native_frame.media_type,
                        "native_width": native_frame.width,
                        "native_height": native_frame.height,
                        "native_bytes": len(native_frame.data),
                        "native_sha256": hashlib.sha256(
                            native_frame.data
                        ).hexdigest(),
                    }
                )
            else:
                record["native_passthrough_available"] = False
            image_records.append(record)
        report = {
            "generated_at_unix": time.time(),
            "read_only": True,
            "image_archive_contract": (
                "exact JPEG bytes produced by the production multipart HTTP "
                "encoder from the captured RGB arrays; files are ordered "
                "head, left_wrist, right_wrist"
            ),
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

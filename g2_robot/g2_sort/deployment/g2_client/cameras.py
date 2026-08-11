"""G2三路相机的严格RGB采集适配。

用途：从GDK依次读取头、左腕、右腕图像，并统一解码为RGB HWC uint8。
输入：``agibot_gdk.Camera``图像消息、相机类型和单路读取超时。
输出：固定顺序的三个RGB numpy数组；任一路异常都抛出``ObservationError``。
方法：按JPEG/PNG/未压缩格式解码，不resize，也不使用黑图替代失败帧。
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np

from .contracts import ObservationError


class G2CameraRig:
    """Capture head, left-wrist, and right-wrist frames in contract order."""

    def __init__(
        self,
        timeout_ms: float = 200.0,
        stabilization_seconds: float = 3.0,
        *,
        gdk_module: Any | None = None,
        camera: Any | None = None,
    ) -> None:
        if timeout_ms <= 0:
            raise ValueError("camera timeout must be positive")
        if gdk_module is None:
            try:
                import agibot_gdk as gdk_module
            except ImportError as exc:  # pragma: no cover - robot-only dependency
                raise ObservationError(
                    "agibot_gdk is required for real G2 camera capture"
                ) from exc
        self._gdk = gdk_module
        self._camera = camera if camera is not None else gdk_module.Camera()
        self._timeout_ms = float(timeout_ms)
        self._channels = (
            ("head", gdk_module.CameraType.kHeadColor),
            ("left_wrist", gdk_module.CameraType.kHandLeftColor),
            ("right_wrist", gdk_module.CameraType.kHandRightColor),
        )
        if stabilization_seconds > 0:
            time.sleep(stabilization_seconds)

    def capture(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        images = [self.capture_channel(name) for name, _ in self._channels]
        return tuple(images)  # type: ignore[return-value]

    def capture_channel(self, name: str) -> np.ndarray:
        """Read one named channel for diagnostics while preserving strict decode."""
        camera_types = dict(self._channels)
        if name not in camera_types:
            raise ObservationError(f"unknown camera channel {name!r}")
        try:
            message = self._camera.get_latest_image(
                camera_types[name], self._timeout_ms
            )
        except Exception as exc:
            raise ObservationError(f"camera {name} read failed: {exc}") from exc
        if message is None:
            raise ObservationError(f"camera {name} returned no frame")
        return self._decode_rgb(message, name)

    def _decode_rgb(self, message: Any, name: str) -> np.ndarray:
        try:
            import cv2
        except ImportError as exc:  # pragma: no cover - robot-only dependency
            raise ObservationError("opencv-python is required for G2 cameras") from exc
        try:
            encoding = message.encoding
            if encoding in (self._gdk.Encoding.JPEG, self._gdk.Encoding.PNG):
                buffer = np.frombuffer(message.data, dtype=np.uint8)
                bgr = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
                if bgr is None:
                    raise ObservationError(f"camera {name} compressed decode failed")
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            elif encoding == self._gdk.Encoding.UNCOMPRESSED:
                height = int(message.height)
                width = int(message.width)
                if height <= 0 or width <= 0:
                    raise ObservationError(
                        f"camera {name} invalid dimensions {width}x{height}"
                    )
                color_format = message.color_format
                if color_format == self._gdk.ColorFormat.RGB:
                    rgb = np.frombuffer(message.data, dtype=np.uint8).reshape(
                        height, width, 3
                    )
                elif color_format == self._gdk.ColorFormat.BGR:
                    bgr = np.frombuffer(message.data, dtype=np.uint8).reshape(
                        height, width, 3
                    )
                    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                elif color_format == self._gdk.ColorFormat.GRAY8:
                    gray = np.frombuffer(message.data, dtype=np.uint8).reshape(
                        height, width
                    )
                    rgb = cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)
                elif color_format == self._gdk.ColorFormat.GRAY16:
                    gray16 = np.frombuffer(
                        message.data, dtype=np.uint16
                    ).reshape(height, width)
                    gray8 = (gray16 >> 8).astype(np.uint8)
                    rgb = cv2.cvtColor(gray8, cv2.COLOR_GRAY2RGB)
                else:
                    raise ObservationError(
                        f"camera {name} unsupported color format {color_format}"
                    )
            else:
                raise ObservationError(
                    f"camera {name} unsupported encoding {encoding}"
                )
        except ObservationError:
            raise
        except Exception as exc:
            raise ObservationError(f"camera {name} decode failed: {exc}") from exc
        if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
            raise ObservationError(
                f"camera {name} decoded invalid RGB shape/dtype "
                f"{rgb.shape}/{rgb.dtype}"
            )
        return np.ascontiguousarray(rgb)

    def close(self) -> None:
        close = getattr(self._camera, "close_camera", None)
        if callable(close):
            close()

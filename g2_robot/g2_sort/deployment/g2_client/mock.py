"""无GDK环境使用的显式mock相机。

用途：让GPU服务器或CI能够测试HTTP合同、调度和shadow路径。
输入：可选图像高度和宽度。
输出：固定顺序的三张确定性RGB HWC uint8图像。
方法：生成不同通道梯度以区分三路图；这是测试输入，不是相机失败黑图回退。
"""

from __future__ import annotations

from io import BytesIO

import numpy as np

from .image_transport import EncodedImage


class MockCameraRig:
    """Return three deterministic, valid RGB frames (never a failure fallback)."""

    def __init__(self, height: int = 96, width: int = 128) -> None:
        if height <= 0 or width <= 0:
            raise ValueError("mock image dimensions must be positive")
        y = np.arange(height, dtype=np.uint8)[:, None]
        x = np.arange(width, dtype=np.uint8)[None, :]
        self._images = (
            np.stack(
                (
                    np.broadcast_to(x, (height, width)),
                    np.broadcast_to(y, (height, width)),
                    np.full((height, width), 64, dtype=np.uint8),
                ),
                axis=2,
            ),
            np.stack(
                (
                    np.full((height, width), 96, dtype=np.uint8),
                    np.broadcast_to(x, (height, width)),
                    np.broadcast_to(y, (height, width)),
                ),
                axis=2,
            ),
            np.stack(
                (
                    np.broadcast_to(y, (height, width)),
                    np.full((height, width), 160, dtype=np.uint8),
                    np.broadcast_to(x, (height, width)),
                ),
                axis=2,
            ),
        )

    def capture(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        return tuple(image.copy() for image in self._images)  # type: ignore[return-value]

    def capture_encoded(self) -> tuple[EncodedImage, EncodedImage, EncodedImage]:
        from PIL import Image

        frames = []
        for image in self._images:
            output = BytesIO()
            Image.fromarray(image, mode="RGB").save(output, format="JPEG", quality=95)
            frames.append(
                EncodedImage(
                    output.getvalue(),
                    "image/jpeg",
                    int(image.shape[1]),
                    int(image.shape[0]),
                )
            )
        return tuple(frames)  # type: ignore[return-value]

    def close(self) -> None:
        return None

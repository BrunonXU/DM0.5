"""可插拔G2策略后端的最小接口。

用途：让控制循环不依赖DM0.5或未来模型的具体通信协议。
输入：已经通过公共合同校验的``Observation``。
输出：带原request ID和时间信息的严格``ActionChunk``。
方法：后端只实现同步``infer``和``close``，不得读取GDK或直接控制servo。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .contracts import ActionChunk, Observation


@runtime_checkable
class PolicyBackend(Protocol):
    """A model-protocol adapter with no dependency on GDK or servo control."""

    def infer(self, observation: Observation) -> ActionChunk:
        """Return one validated action chunk for ``observation``."""

    def close(self) -> None:
        """Release network resources.  Implementations may make this a no-op."""

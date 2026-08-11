"""解耦后的DM0.5 G2客户端公共包。

用途：导出所有后端共同使用的34D、50步、30/100Hz和数据类型合同。
输入：包导入本身没有运行时输入，也不会初始化任何机器人硬件。
输出：``Observation``、``ActionChunk``及冻结常量。
方法：GDK只在真实相机/机器人对象创建时延迟导入，使纯逻辑可在服务器测试。
"""

from .contracts import (
    ACTION_DIM,
    CHUNK_STEPS,
    MODEL_HZ,
    SERVO_HZ,
    ActionChunk,
    Observation,
)

__all__ = [
    "ACTION_DIM",
    "CHUNK_STEPS",
    "MODEL_HZ",
    "SERVO_HZ",
    "ActionChunk",
    "Observation",
]

"""DM0.5动作维度到G2关节的冻结映射与位置限位。

用途：建立训练合同34D与GDK真实关节名的一一对应，并集中保存物理范围。
输入：单个34D动作或最后一维为34的动作chunk。
输出：策略关节顺序、14D双臂/20D双手切分、裁剪动作及裁剪统计。
方法：策略顺序固定为左臂7、右臂7、左手10、右手10；正式硬件层按前14D
Servo与后20D O10切分，历史17D映射只供显式诊断。
"""

from __future__ import annotations

from typing import Final

import numpy as np

from .contracts import ACTION_DIM, ContractError

LEFT_ARM_JOINT_NAMES: Final[tuple[str, ...]] = tuple(
    f"idx2{i}_arm_l_joint{i}" for i in range(1, 8)
)
RIGHT_ARM_JOINT_NAMES: Final[tuple[str, ...]] = tuple(
    f"idx6{i}_arm_r_joint{i}" for i in range(1, 8)
)
LEFT_HAND_JOINT_NAMES: Final[tuple[str, ...]] = (
    "idx31_hand_l_thumb_roll_joint",
    "idx32_hand_l_thumb_abad_joint",
    "idx33_hand_l_thumb_mcp_joint",
    "idx36_hand_l_index_abad_joint",
    "idx37_hand_l_index_pip_joint",
    "idx39_hand_l_middle_pip_joint",
    "idx41_hand_l_ring_abad_joint",
    "idx42_hand_l_ring_pip_joint",
    "idx44_hand_l_pinky_abad_joint",
    "idx45_hand_l_pinky_pip_joint",
)
RIGHT_HAND_JOINT_NAMES: Final[tuple[str, ...]] = (
    "idx71_hand_r_thumb_roll_joint",
    "idx72_hand_r_thumb_abad_joint",
    "idx73_hand_r_thumb_mcp_joint",
    "idx76_hand_r_index_abad_joint",
    "idx77_hand_r_index_pip_joint",
    "idx79_hand_r_middle_pip_joint",
    "idx81_hand_r_ring_abad_joint",
    "idx82_hand_r_ring_pip_joint",
    "idx84_hand_r_pinky_abad_joint",
    "idx85_hand_r_pinky_pip_joint",
)
POLICY_JOINT_NAMES: Final[tuple[str, ...]] = (
    LEFT_ARM_JOINT_NAMES
    + RIGHT_ARM_JOINT_NAMES
    + LEFT_HAND_JOINT_NAMES
    + RIGHT_HAND_JOINT_NAMES
)

# The model/state contract above must never be reordered.  GDK 2.6.3 o10_t2
# servo requests use a different boundary contract: an end effector is sent
# together with the seven joints of its corresponding arm.
LEFT_GDK_SERVO_JOINT_NAMES: Final[tuple[str, ...]] = (
    LEFT_ARM_JOINT_NAMES + LEFT_HAND_JOINT_NAMES
)
RIGHT_GDK_SERVO_JOINT_NAMES: Final[tuple[str, ...]] = (
    RIGHT_ARM_JOINT_NAMES + RIGHT_HAND_JOINT_NAMES
)
LEFT_GDK_SERVO_POLICY_INDICES: Final[tuple[int, ...]] = (
    tuple(range(0, 7)) + tuple(range(14, 24))
)
RIGHT_GDK_SERVO_POLICY_INDICES: Final[tuple[int, ...]] = (
    tuple(range(7, 14)) + tuple(range(24, 34))
)

_ARM_LIMITS = (
    (-3.071796, 3.071796),
    (-2.059505, 2.059505),
    (-3.071796, 3.071796),
    (-2.495838, 1.012308),
    (-3.071796, 3.071796),
    (-1.012308, 1.012308),
    (-1.535907, 1.535907),
)
_LEFT_HAND_LIMITS = (
    (-1.1213740444063567, 0.029670597283903602),
    (-0.04537856055185257, 1.642354826126664),
    (-0.8415977653116657, 0.0),
    (0.0, 0.16406094968746698),
    (0.0, 1.4835298641951802),
    (0.0, 1.4835298641951802),
    (-0.16929693744344995, 0.0),
    (0.0, 1.4835298641951802),
    (-0.1850049007113989, 0.0),
    (0.0, 1.4835298641951802),
)
_RIGHT_HAND_LIMITS = (
    (-0.029670597283903602, 1.1213740444063567),
    (-1.642354826126664, 0.04537856055185257),
    (0.0, 0.8415977653116657),
    (-0.16406094968746698, 0.0),
    (0.0, 1.4835298641951802),
    (0.0, 1.4835298641951802),
    (0.0, 0.16929693744344995),
    (0.0, 1.4835298641951802),
    (0.0, 0.1850049007113989),
    (0.0, 1.4835298641951802),
)
POLICY_JOINT_LIMITS: Final[np.ndarray] = np.asarray(
    _ARM_LIMITS + _ARM_LIMITS + _LEFT_HAND_LIMITS + _RIGHT_HAND_LIMITS,
    dtype=np.float64,
)

assert len(POLICY_JOINT_NAMES) == ACTION_DIM
assert POLICY_JOINT_LIMITS.shape == (ACTION_DIM, 2)
assert len(LEFT_GDK_SERVO_JOINT_NAMES) == 17
assert len(RIGHT_GDK_SERVO_JOINT_NAMES) == 17
assert (
    set(LEFT_GDK_SERVO_JOINT_NAMES + RIGHT_GDK_SERVO_JOINT_NAMES)
    == set(POLICY_JOINT_NAMES)
)


def clip_action_positions(
    actions: np.ndarray,
) -> tuple[np.ndarray, int, float]:
    """Clip one action or a chunk to physical G2 position limits."""
    array = np.asarray(actions, dtype=np.float64)
    if array.shape[-1:] != (ACTION_DIM,):
        raise ContractError(
            f"action last dimension must be {ACTION_DIM}, got {array.shape}"
        )
    if not np.all(np.isfinite(array)):
        raise ContractError("cannot limit non-finite action positions")
    clipped = np.clip(
        array,
        POLICY_JOINT_LIMITS[:, 0],
        POLICY_JOINT_LIMITS[:, 1],
    )
    delta = np.abs(clipped - array)
    count = int(np.count_nonzero(delta > 0.0))
    max_delta = float(np.max(delta)) if delta.size else 0.0
    return clipped, count, max_delta


def split_policy_action_for_gdk_servo(
    action: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Historical diagnostic mapping from policy 34D into two 17D groups.

    This helper is not the production Split path. Production dispatches the
    first 14 arm values through 100 Hz Servo and the last 20 O10 values through
    5 Hz ``move_ee_pos``; it never rebuilds two 17D arm-plus-hand requests.
    """
    array = np.asarray(action, dtype=np.float64)
    if array.shape != (ACTION_DIM,):
        raise ContractError(
            f"GDK servo target must be ({ACTION_DIM},), got {array.shape}"
        )
    if not np.all(np.isfinite(array)):
        raise ContractError("cannot map non-finite GDK servo positions")
    return (
        array[np.asarray(LEFT_GDK_SERVO_POLICY_INDICES, dtype=np.intp)],
        array[np.asarray(RIGHT_GDK_SERVO_POLICY_INDICES, dtype=np.intp)],
    )

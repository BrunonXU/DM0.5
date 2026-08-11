"""G2 DM0.5运行时唯一的冻结数据合同。

用途：集中定义prompt、34D顺序、H5字段和powered安全默认值。
输入：训练数据合同及离线统计结果；导入模块时没有运行时输入。
输出：供训练、checkpoint验证和部署复用的冻结常量。
方法：所有模块只导入本文件，不复制prompt、维度顺序或安全阈值。
"""

from __future__ import annotations

ACTION_DIM = 34
PROMPT = (
    "Use the left hand to pick up each bottle, hand it to the right hand, "
    "then place the Sprite bottle in the left tray compartment and the orange "
    "juice bottle in the right tray compartment."
)

ARM_JOINT_NAMES = [
    *(f"arm_l_joint{i}" for i in range(1, 8)),
    *(f"arm_r_joint{i}" for i in range(1, 8)),
]
HAND_DOF_NAMES = [
    "thumb_roll",
    "thumb_abad",
    "thumb_mcp",
    "index_abad",
    "index_pip",
    "middle_pip",
    "ring_abad",
    "ring_pip",
    "pinky_abad",
    "pinky_pip",
]
ACTION_NAMES = [
    *ARM_JOINT_NAMES,
    *(f"hand_l_{name}" for name in HAND_DOF_NAMES),
    *(f"hand_r_{name}" for name in HAND_DOF_NAMES),
]

STATE_PATHS = (
    "state/joint/position",
    "state/left_effector/position",
    "state/right_effector/position",
)
ACTION_PATHS = (
    "action/joint/position",
    "action/left_effector/position",
    "action/right_effector/position",
)
CAMERAS = {
    "images_head": ("head_color", "camera/head_color/head_color.h265"),
    "images_left_wrist": (
        "hand_left_color",
        "camera/hand_left_color/hand_left_color.h265",
    ),
    "images_right_wrist": (
        "hand_right_color",
        "camera/hand_right_color/hand_right_color.h265",
    ),
}
CAMERA_TIMESTAMP_FILES = {
    image_key: relative_path.replace(".h265", ".txt")
    for image_key, (_, relative_path) in CAMERAS.items()
}

# Powered gates are intentionally below rare discontinuous training outliers.
# Reference statistics were streamed over all consecutive action frames in the
# frozen 800-episode train split on 2026-07-31.
POWERED_SAFETY_DEFAULTS = {
    "max_clip_delta_radians": 0.02,
    "max_arm_velocity_rad_s": 3.0,
    "max_hand_velocity_rad_s": 8.0,
    "max_arm_acceleration_rad_s2": 120.0,
    "max_hand_acceleration_rad_s2": 300.0,
    "max_jerk_rad_s3": 5000.0,
}
TRAINING_DYNAMICS_REFERENCE = {
    "episode_count": 800,
    "model_hz": 30.0,
    "frame_max_velocity_rad_s_p95_p99_p999": [
        1.6470587253570557,
        3.176470398902893,
        5.699999928474426,
    ],
    "frame_max_acceleration_rad_s2_p95_p99_p999": [
        28.235292434692383,
        67.05881953239441,
        162.3529314994812,
    ],
    "frame_max_jerk_rad_s3_p95_p99_p999": [
        1270.5881595611572,
        2970.0007885694504,
        7411.7642641067505,
    ],
}

assert len(ACTION_NAMES) == ACTION_DIM

"""第一版 G2 DM0.5 基线的冻结数据合同。

集中定义固定英文 prompt、34D 关节顺序以及 H5 state/action 字段来源。训练、
checkpoint验证和部署必须复用同一顺序，不得各自维护副本。本文件属于 G2
项目定制代码。
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

assert len(ACTION_NAMES) == ACTION_DIM

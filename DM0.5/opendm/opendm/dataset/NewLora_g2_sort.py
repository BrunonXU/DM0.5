"""Register the isolated NewLora G2 mixed-action datasets.

The first 14 arm dimensions are delta encoded.  The final 20 dexterous-hand
dimensions remain absolute by marking them as GRIPPER for OpenDM's existing
per-dimension relative-action transform.
"""

import os

from opendm.constants.robot import RobotStateDesc
from opendm.dataset.register import register_dataset

NEWLORA_ARM_DIM = 14
NEWLORA_HAND_DIM = 20
NEWLORA_ACTION_DIM = NEWLORA_ARM_DIM + NEWLORA_HAND_DIM
NEWLORA_ROOT = os.getenv("G2_SORT_DATA_ROOT", "./data/g2_sort")
NEWLORA_STATE_DESC = (
    [RobotStateDesc.JOINT] * NEWLORA_ARM_DIM
    + [RobotStateDesc.GRIPPER] * NEWLORA_HAND_DIM
)
NEWLORA_IMAGE_KEYS = [
    "images_head",
    "images_left_wrist",
    "images_right_wrist",
]

register_dataset(
    {
        "NewLora_g2_sort_train": {
            "jsonl_dir": os.path.join(NEWLORA_ROOT, "jsonl", "train"),
            "image_dir": os.path.join(NEWLORA_ROOT, "videos"),
            "image_keys": NEWLORA_IMAGE_KEYS,
            "state_desc": NEWLORA_STATE_DESC,
        },
        "NewLora_g2_sort_validation": {
            "jsonl_dir": os.path.join(NEWLORA_ROOT, "jsonl", "validation"),
            "image_dir": os.path.join(NEWLORA_ROOT, "videos"),
            "image_keys": NEWLORA_IMAGE_KEYS,
            "state_desc": NEWLORA_STATE_DESC,
        },
    }
)

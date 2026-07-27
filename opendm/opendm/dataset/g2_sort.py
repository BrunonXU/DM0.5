"""将 G2 双臂灵巧手分拣数据注册到 OpenDM。

这是项目专用的最小桥接层，不属于OpenDM上游。它只声明JSONL、视频目录、
三路图像键和34D state描述；原始数据审计与转换位于项目根目录 ``g2_sort``。
"""

import os

from opendm.constants.robot import RobotStateDesc
from opendm.dataset.register import register_dataset

G2_SORT_ACTION_DIM = 34
G2_SORT_ROOT = os.getenv("G2_SORT_DATA_ROOT", "./data/g2_sort")

register_dataset(
    {
        "g2_sort_train": {
            "jsonl_dir": os.path.join(G2_SORT_ROOT, "jsonl", "train"),
            "image_dir": os.path.join(G2_SORT_ROOT, "videos"),
            "image_keys": ["images_head", "images_left_wrist", "images_right_wrist"],
            "state_desc": [RobotStateDesc.JOINT] * G2_SORT_ACTION_DIM,
        },
        "g2_sort_validation": {
            "jsonl_dir": os.path.join(G2_SORT_ROOT, "jsonl", "validation"),
            "image_dir": os.path.join(G2_SORT_ROOT, "videos"),
            "image_keys": ["images_head", "images_left_wrist", "images_right_wrist"],
            "state_desc": [RobotStateDesc.JOINT] * G2_SORT_ACTION_DIM,
        },
    }
)

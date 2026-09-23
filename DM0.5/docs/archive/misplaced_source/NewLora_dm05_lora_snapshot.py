"""ARCHIVED SNAPSHOT ONLY.

This Python source was accidentally saved under docs with a .md suffix. The maintained
source is g2_sort/training/NewLora_dm05_lora.py. Do not import or edit this snapshot.

G2 双臂灵巧手饮料瓶分拣的 DM0.5 LoRA 训练配置。

输入三路RGB、34D measured state、34D absolute-joint action和固定英文prompt；
输出50步、34D action chunk。第一版使用无主动增强的补边+448×448图像管线。
服务器正式训练使用官方模型实现，并通过命令行固定经RTX 6000D backward
验证的SDPA/FlashAttention2/SDPA组合。本文件属于G2项目定制代码。
"""

import os
from dataclasses import dataclass, field
from typing import Literal

import tyro

import opendm.dataset.g2_sort  # noqa: F401  # Register G2 dataset definitions.
from opendm.constants.robot import ActionMode
from opendm.data.augmentations import NoAugmentationPipeline, TrainingTransformPipeline
from opendm.data.collator import TrainingCollator
from opendm.data.dataset import JsonlDataset
from opendm.data.transforms import (
    ChatTokenization,
    LoadImages,
    Normalize,
    PadAction,
    Pipeline,
    PixelTransform,
)
from opendm.exp.dm05_exp import (
    DM05DataConfig as _DM05DataConfig,
)
from opendm.exp.dm05_exp import (
    DM05Exp as _DM05Exp,
)
from opendm.exp.dm05_exp import (
    DM05InferenceConfig as _DM05InferenceConfig,
)
from opendm.exp.dm05_exp import (
    DM05ModelConfig as _DM05ModelConfig,
)
from opendm.exp.dm05_exp import (
    DM05OptimizerConfig as _DM05OptimizerConfig,
)
from opendm.exp.dm05_exp import (
    DM05TrainerConfig as _DM05TrainerConfig,
)

ACTION_DIM = 34
IMAGE_KEYS = ["images_head", "images_left_wrist", "images_right_wrist"]


@dataclass
class DM05DataConfig(_DM05DataConfig):
    dataset_name: str = field(default="g2_sort_train")
    action_mode: ActionMode = field(default=ActionMode.RELATIVE)
    add_state: bool = field(default=True)
    action_dim: int = field(default=ACTION_DIM)

    def build_dataset(
        self,
        processor,
        action_horizon: int,
        tokenizer_max_length: int = 1024,
    ) -> tuple:
        dataset_info = self._dataset_info()
        image_keys = dataset_info["image_keys"]
        pipeline = Pipeline(
            [
                self._action_transform(action_horizon),
                LoadImages(image_keys=image_keys, image_dir=dataset_info["image_dir"]),
                PixelTransform(
                    transform_pipeline=TrainingTransformPipeline(p=0.5),
                    image_keys=image_keys,
                ),
                Normalize(
                    norm_stats_path=str(self.norm_stats_path(action_horizon)),
                    norm_keys=["state", "action"],
                    use_quantiles=True,
                ),
                ChatTokenization(
                    processor=processor,
                    n_bins=self.n_bins,
                    max_length=tokenizer_max_length,
                    image_keys=image_keys,
                    add_state=self.add_state,
                ),
                PadAction(self.action_dim),
            ]
        )
        dataset = JsonlDataset(
            jsonl_dir=dataset_info["jsonl_dir"],
            transforms=pipeline,
            dataset_name=self.dataset_name,
            dataset_meta=self._dataset_meta(dataset_info),
        )
        collator = TrainingCollator(
            pad_token_id=processor.tokenizer.pad_token_id,
            max_length=tokenizer_max_length,
        )
        return dataset, collator


@dataclass
class DM05ModelConfig(_DM05ModelConfig):
    model_name_or_path: str | None = field(default="./checkpoints/DM05")
    action_dim: int = field(default=ACTION_DIM)
    chunk_size: int = field(default=50)
    llm_attn_implementation: Literal["auto", "eager", "sdpa", "flex_attention"] = (
        field(default="flex_attention")
    )
    vision_attn_implementation: Literal[
        "auto", "eager", "sdpa", "flash_attention_2"
    ] = field(default="flash_attention_2")
    action_attn_implementation: Literal[
        "auto", "eager", "sdpa", "flex_attention"
    ] = field(default="sdpa")


@dataclass
class DM05OptimizerConfig(_DM05OptimizerConfig):
    base_lr: float = field(default=5e-4)
    warmup_steps: int = field(default=1000)


@dataclass
class DM05TrainerConfig(_DM05TrainerConfig):
    output_dir: str = field(
        default=f"user_checkpoints/{os.path.basename(__file__)[:-3]}"
    )
    fsdp1: bool | None = field(default=False)
    per_device_train_batch_size: int = field(default=4)
    gradient_accumulation_steps: int = field(default=1)
    save_steps: int = field(default=5000)
    num_train_steps: int = field(default=50000)
    save_only_model: bool = field(default=True)


@dataclass
class DM05InferenceConfig(_DM05InferenceConfig):
    output_action_dim: int = field(default=ACTION_DIM)
    image_keys: list[str] = field(default_factory=lambda: list(IMAGE_KEYS))


@dataclass
class DM05Exp(_DM05Exp):
    use_lora: bool | None = field(default=True)
    model_config: DM05ModelConfig = field(default_factory=DM05ModelConfig)
    optimizer_config: DM05OptimizerConfig = field(default_factory=DM05OptimizerConfig)
    trainer_config: DM05TrainerConfig = field(default_factory=DM05TrainerConfig)
    data_config: DM05DataConfig = field(default_factory=DM05DataConfig)
    inference_config: DM05InferenceConfig = field(default_factory=DM05InferenceConfig)

    def __post_init__(self):
        super().__post_init__()
        if self.inference_config.output_action_dim != self.model_config.action_dim:
            raise ValueError(
                "G2 inference must expose all 34 action dimensions: "
                f"output={self.inference_config.output_action_dim}, "
                f"model={self.model_config.action_dim}"
            )


if __name__ == "__main__":
    exp = tyro.cli(DM05Exp)
    if exp.task == "train":
        exp.train()
    elif exp.task == "inference":
        exp.inference()
    else:
        raise ValueError(f"Invalid task: {exp.task}")

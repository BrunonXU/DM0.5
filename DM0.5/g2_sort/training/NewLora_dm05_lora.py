"""NewLora: G2 arm-relative, dexterous-hand-absolute DM0.5 LoRA.

This experiment applies the vendor-recommended full training augmentation to
address the observed collection-to-rollout scene and illumination gap. It also
uses mixed action semantics: the 14 arm dimensions are relative and the 20
dexterous-hand dimensions remain absolute. All artifacts are isolated with the
``NewLora`` prefix.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from io import BytesIO
from typing import Literal

import numpy as np
import torch
import tyro
from flask import request
from PIL import Image

import opendm.dataset.NewLora_g2_sort  # noqa: F401
from g2_sort.data_tools.g2_contract import PROMPT
from g2_sort.transport.dm05_binary import BinaryRequest
from opendm.constants.robot import ActionMode
from opendm.data.augmentations import TrainingTransformPipeline
from opendm.data.collator import TrainingCollator
from opendm.data.dataset import JsonlDataset
from opendm.data.transforms import (
    ActionRelative,
    ChatTokenization,
    LoadImages,
    Normalize,
    PadAction,
    Pipeline,
    PixelTransform,
)
from opendm.dataset.NewLora_g2_sort import (
    NEWLORA_ACTION_DIM,
    NEWLORA_IMAGE_KEYS,
    NEWLORA_STATE_DESC,
)
from opendm.exp.dm05_exp import DM05DataConfig as _DM05DataConfig
from opendm.exp.dm05_exp import DM05Exp as _DM05Exp
from opendm.exp.dm05_exp import DM05InferenceConfig as _DM05InferenceConfig
from opendm.exp.dm05_exp import DM05ModelConfig as _DM05ModelConfig
from opendm.exp.dm05_exp import DM05OptimizerConfig as _DM05OptimizerConfig
from opendm.exp.dm05_exp import DM05TrainerConfig as _DM05TrainerConfig
from opendm.model.dm05.dm05_lora import unwrap_dm05_model

ACTION_DIM = NEWLORA_ACTION_DIM
IMAGE_KEYS = list(NEWLORA_IMAGE_KEYS)


@dataclass
class DM05DataConfig(_DM05DataConfig):
    dataset_name: str = field(default="NewLora_g2_sort_train")
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
    llm_attn_implementation: Literal["auto", "eager", "sdpa", "flex_attention"] = field(default="flex_attention")
    vision_attn_implementation: Literal["auto", "eager", "sdpa", "flash_attention_2"] = field(default="flash_attention_2")
    action_attn_implementation: Literal["auto", "eager", "sdpa", "flex_attention"] = field(default="sdpa")


@dataclass
class DM05OptimizerConfig(_DM05OptimizerConfig):
    base_lr: float = field(default=5e-4)
    warmup_steps: int = field(default=1000)


@dataclass
class DM05TrainerConfig(_DM05TrainerConfig):
    output_dir: str = field(default="user_checkpoints/NewLora_officialaug_dm05_lora")
    fsdp1: bool | None = field(default=False)
    per_device_train_batch_size: int = field(default=4)
    gradient_accumulation_steps: int = field(default=2)
    save_steps: int = field(default=5000)
    save_total_limit: int = field(default=15)
    num_train_steps: int = field(default=75000)
    save_only_model: bool = field(default=False)
    wandb_project: str | None = field(default="NewLora_DM05_G2")


@dataclass
class DM05InferenceConfig(_DM05InferenceConfig):
    output_action_dim: int = field(default=ACTION_DIM)
    image_keys: list[str] = field(default_factory=lambda: list(IMAGE_KEYS))

    def _initialize(self, *args, norm_stats_path: str, **kwargs) -> None:
        super()._initialize(*args, norm_stats_path=norm_stats_path, **kwargs)
        self._image_decode_executor = ThreadPoolExecutor(
            max_workers=len(self.image_keys),
            thread_name_prefix="newlora-image-decode",
        )
        self._rtc_action_normalize = Normalize(
            norm_stats_path=norm_stats_path,
            norm_keys=["action"],
            use_quantiles=True,
        )
        self._rtc_action_relative = ActionRelative()

    @staticmethod
    def _decode_uploaded_rgb(image) -> Image.Image:
        with Image.open(image) as opened:
            return opened.convert("RGB")

    def _prepare_input(
        self,
        timing_seconds: dict[str, float] | None = None,
    ) -> dict:
        timing = timing_seconds if timing_seconds is not None else {}
        form_started = time.monotonic()
        images = request.files.getlist("image", None)
        states = request.form.get("states", None)
        text = request.form.get("text", "")
        speed = request.form.get("speed", "0.5")
        control_mode = request.form.get("control_mode")
        timing["server_multipart_form_access_seconds"] = (
            time.monotonic() - form_started
        )
        if len(images) != len(self.image_keys):
            raise ValueError(
                f"NewLora G2 expects {len(self.image_keys)} images, got {len(images)}"
            )
        state_started = time.monotonic()
        state = np.array(json.loads(states), dtype=np.float32)
        timing["server_state_json_parse_seconds"] = (
            time.monotonic() - state_started
        )
        if state.shape != (ACTION_DIM,):
            raise ValueError(
                f"NewLora G2 expects state shape ({ACTION_DIM},), got {state.shape}"
            )
        decode_started = time.monotonic()
        decoded_images = list(
            self._image_decode_executor.map(self._decode_uploaded_rgb, images)
        )
        timing["server_image_decode_seconds"] = time.monotonic() - decode_started
        pil_images = dict(zip(self.image_keys, decoded_images, strict=True))
        rtc_parse_started = time.monotonic()
        rtc_fields = {
            "prefix": request.form.get("rtc_prefix_actions"),
            "delay": request.form.get("rtc_inference_delay_steps"),
            "execution": request.form.get("rtc_execution_horizon"),
        }
        rtc_present = {key: value is not None for key, value in rtc_fields.items()}
        if any(rtc_present.values()) and not all(rtc_present.values()):
            raise ValueError(
                "RTC request must provide prefix, inference delay, and execution horizon"
            )
        rtc = None
        if all(rtc_present.values()):
            if not self.rtc_enabled:
                raise ValueError("RTC request rejected because rtc_enabled is false")
            prefix = np.asarray(json.loads(rtc_fields["prefix"]), dtype=np.float32)
            if (
                prefix.ndim != 2
                or prefix.shape[1] != ACTION_DIM
                or not 1 <= prefix.shape[0] <= 50
                or not np.all(np.isfinite(prefix))
            ):
                raise ValueError(
                    "RTC prefix must be a finite [1..50, 34] absolute trajectory"
                )
            delay = int(rtc_fields["delay"])
            execution = int(rtc_fields["execution"])
            if not 0 <= delay <= prefix.shape[0]:
                raise ValueError("RTC delay must fit inside the committed prefix")
            if not delay <= execution <= 50 - delay:
                raise ValueError(
                    "RTC scheduling requires delay <= execution <= 50 - delay"
                )
            expected_prefix_steps = 50 - execution
            if prefix.shape[0] != expected_prefix_steps:
                raise ValueError(
                    "RTC prefix length must equal 50 - execution_horizon: "
                    f"got {prefix.shape[0]} vs {expected_prefix_steps}"
                )
            rtc = {
                "absolute_prefix": prefix,
                "inference_delay": delay,
                "execution_horizon": execution,
            }
        timing["server_rtc_form_parse_seconds"] = (
            time.monotonic() - rtc_parse_started
        )
        timing["server_request_parse_seconds"] = (
            timing["server_multipart_form_access_seconds"]
            + timing["server_state_json_parse_seconds"]
            + timing["server_rtc_form_parse_seconds"]
        )
        return {
            **pil_images,
            "prompt": text,
            "state": state,
            "meta_data": {
                # Match the training prompt: G2's compact dataset does not add
                # a Robot: line, while state_desc is still required to invert
                # the mixed relative representation.
                "robot_type": None,
                "speed": str(speed),
                "control_mode": control_mode,
                "state_desc": list(NEWLORA_STATE_DESC),
            },
            "rtc": rtc,
        }

    def _prepare_binary_input(
        self,
        binary: BinaryRequest,
        timing_seconds: dict[str, float] | None = None,
    ) -> dict:
        """Build the identical model input from an already validated wire request."""
        timing = timing_seconds if timing_seconds is not None else {}
        state = np.asarray(binary.state, dtype=np.float32)
        if state.shape != (ACTION_DIM,) or not np.all(np.isfinite(state)):
            raise ValueError("NewLora binary state must be finite 34D")

        decode_started = time.monotonic()
        decoded_images = list(
            self._image_decode_executor.map(
                self._decode_uploaded_rgb,
                (BytesIO(image.data) for image in binary.images),
            )
        )
        timing["server_image_decode_seconds"] = time.monotonic() - decode_started

        rtc = None
        if binary.rtc_prefix is not None:
            if not self.rtc_enabled:
                raise ValueError("RTC request rejected because rtc_enabled is false")
            rtc = {
                "absolute_prefix": np.asarray(binary.rtc_prefix, dtype=np.float32),
                "inference_delay": int(binary.rtc_delay),
                "execution_horizon": int(binary.rtc_execution),
            }
        return {
            **dict(zip(self.image_keys, decoded_images, strict=True)),
            "prompt": PROMPT,
            "state": state,
            "meta_data": {
                "robot_type": None,
                "speed": "0.5",
                "control_mode": None,
                "state_desc": list(NEWLORA_STATE_DESC),
            },
            "rtc": rtc,
        }

    def _prepare_rtc_model_kwargs(
        self,
        data: dict,
        *,
        absolute_state: np.ndarray,
    ) -> dict:
        rtc = data.get("rtc")
        if rtc is None:
            return {}
        state = np.asarray(absolute_state, dtype=np.float32)
        if state.shape != (ACTION_DIM,) or not np.all(np.isfinite(state)):
            raise ValueError("RTC absolute state must be a finite 34D vector")
        converted = {
            "state": state.copy(),
            "action": np.asarray(rtc["absolute_prefix"], dtype=np.float32).copy(),
            "meta_data": data["meta_data"],
        }
        converted = self._rtc_action_relative(converted)
        converted = self._rtc_action_normalize(converted)
        prefix = torch.as_tensor(
            converted["action"],
            device=self.device,
            dtype=unwrap_dm05_model(self.model).model.action_in_proj.weight.dtype,
        ).unsqueeze(0)
        return {
            "rtc_prefix_actions": prefix,
            "rtc_inference_delay": int(rtc["inference_delay"]),
            # The model-space overlap is the unexecuted tail of the old chunk.
            "rtc_overlap_steps": int(prefix.shape[1]),
            "rtc_max_guidance_weight": float(self.rtc_max_guidance_weight),
            "rtc_guidance_mode": self.rtc_guidance_mode,
        }


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
        if self.inference_config.output_action_dim != ACTION_DIM:
            raise ValueError("NewLora inference must expose all 34 action dimensions")


if __name__ == "__main__":
    experiment = tyro.cli(DM05Exp)
    if experiment.task == "train":
        experiment.train()
    elif experiment.task == "inference":
        experiment.inference()
    else:
        raise ValueError(f"Invalid task: {experiment.task}")

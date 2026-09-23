"""Shared, deployment-faithful contracts for NewLora validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import opendm.dataset.NewLora_g2_sort  # noqa: F401
from opendm.data.augmentations import NoAugmentationPipeline
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
from opendm.dataset.NewLora_g2_sort import (
    NEWLORA_ACTION_DIM,
    NEWLORA_ARM_DIM,
    NEWLORA_HAND_DIM,
    NEWLORA_IMAGE_KEYS,
    NEWLORA_STATE_DESC,
)
from g2_sort.training.NewLora_dm05_lora import (
    DM05DataConfig as _NewLoraDataConfig,
)
from g2_sort.training.NewLora_dm05_lora import DM05ModelConfig


ACTION_DIM = NEWLORA_ACTION_DIM
ARM_DIM = NEWLORA_ARM_DIM
HAND_DIM = NEWLORA_HAND_DIM
ACTION_HORIZON = 50
GROUPS = {
    "left_arm": slice(0, 7),
    "right_arm": slice(7, 14),
    "left_hand": slice(14, 24),
    "right_hand": slice(24, 34),
    "all": slice(0, 34),
}
HORIZON_INDICES = [0, 4, 9, 19, 24, 49]


def train_norm_path(norm_stats_root: str) -> Path:
    """Return the frozen train-only mixed-action normalization artifact."""
    return _NewLoraDataConfig(
        dataset_name="NewLora_g2_sort_train",
        norm_stats_root=norm_stats_root,
    ).norm_stats_path(action_horizon=ACTION_HORIZON)


@dataclass
class NewLoraValidationDataConfig(_NewLoraDataConfig):
    """NewLora validation data with deterministic, augmentation-free pixels."""

    dataset_name: str = field(default="NewLora_g2_sort_validation")

    def build_dataset(
        self,
        processor,
        action_horizon: int,
        tokenizer_max_length: int = 1024,
    ) -> tuple:
        dataset_info = self._dataset_info()
        image_keys = dataset_info["image_keys"]
        frozen_train_stats = train_norm_path(self.norm_stats_root)
        if not frozen_train_stats.is_file():
            raise FileNotFoundError(
                f"NewLora train-only norm stats not found: {frozen_train_stats}"
            )
        pipeline = Pipeline(
            [
                self._action_transform(action_horizon),
                LoadImages(
                    image_keys=image_keys,
                    image_dir=dataset_info["image_dir"],
                ),
                PixelTransform(
                    transform_pipeline=NoAugmentationPipeline(),
                    image_keys=image_keys,
                ),
                Normalize(
                    norm_stats_path=str(frozen_train_stats),
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


def validate_static_contract() -> None:
    if ACTION_DIM != 34 or ARM_DIM != 14 or HAND_DIM != 20:
        raise RuntimeError(
            f"invalid NewLora dimensions: {ARM_DIM}+{HAND_DIM}!={ACTION_DIM}"
        )
    if len(NEWLORA_STATE_DESC) != ACTION_DIM:
        raise RuntimeError("NewLora state_desc is not 34D")
    if len(NEWLORA_IMAGE_KEYS) != 3:
        raise RuntimeError("NewLora must expose exactly three image keys")


def load_norm_stats(norm_stats_root: str) -> tuple[Path, dict]:
    path = train_norm_path(norm_stats_root)
    payload = json.loads(path.read_text(encoding="utf-8"))
    stats = payload["norm_stats"]
    for key in ("state", "action"):
        for field_name in ("mean", "std", "q01", "q99"):
            values = stats[key][field_name]
            if len(values) != ACTION_DIM:
                raise RuntimeError(
                    f"{path}: {key}.{field_name} has {len(values)} dims, "
                    f"expected {ACTION_DIM}"
                )
    return path, stats


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def denormalize_quantile(array: np.ndarray, stats: dict) -> np.ndarray:
    lo = np.asarray(stats["q01"], dtype=np.float32)
    hi = np.asarray(stats["q99"], dtype=np.float32)
    output = (array + 1.0) / 2.0 * (hi - lo + 1e-6) + lo
    return np.where((lo == 0) & (hi == 0), 0.0, output).astype(np.float32)


def normalize_quantile(array: np.ndarray, stats: dict) -> np.ndarray:
    lo = np.asarray(stats["q01"], dtype=np.float32)
    hi = np.asarray(stats["q99"], dtype=np.float32)
    values = np.asarray(array, dtype=np.float32)
    values = np.clip(values, lo, hi)
    output = (values - lo) / (hi - lo + 1e-6) * 2.0 - 1.0
    return np.where((lo == 0) & (hi == 0), 0.0, output).astype(np.float32)


def mixed_to_absolute(mixed_action: np.ndarray, state: np.ndarray) -> np.ndarray:
    """Invert arm-relative + hand-absolute semantics for a whole chunk."""
    mixed = np.asarray(mixed_action, dtype=np.float32)
    current = np.asarray(state, dtype=np.float32)
    if mixed.shape[-1] != ACTION_DIM or current.shape[-1] != ACTION_DIM:
        raise ValueError(
            f"expected 34D mixed action/state, got {mixed.shape}/{current.shape}"
        )
    absolute = mixed.copy()
    if current.ndim == 1:
        absolute[..., :ARM_DIM] += current[:ARM_DIM]
    else:
        absolute[..., :ARM_DIM] += current[..., None, :ARM_DIM]
    return absolute


def absolute_to_mixed(absolute_action: np.ndarray, state: np.ndarray) -> np.ndarray:
    """Anchor absolute targets in NewLora's 14-relative/20-absolute space."""
    absolute = np.asarray(absolute_action, dtype=np.float32)
    current = np.asarray(state, dtype=np.float32)
    if absolute.shape[-1] != ACTION_DIM or current.shape[-1] != ACTION_DIM:
        raise ValueError(
            f"expected 34D absolute action/state, got {absolute.shape}/{current.shape}"
        )
    mixed = absolute.copy()
    if current.ndim == 1:
        mixed[..., :ARM_DIM] -= current[:ARM_DIM]
    else:
        mixed[..., :ARM_DIM] -= current[..., None, :ARM_DIM]
    return mixed


def build_model(checkpoint: str, device):
    model = DM05ModelConfig(
        model_name_or_path=checkpoint,
        llm_attn_implementation="sdpa",
        vision_attn_implementation="flash_attention_2",
        action_attn_implementation="sdpa",
        liger_kernel=False,
        vlm_gradient_checkpointing=False,
        ae_gradient_checkpointing=False,
    ).build_model(use_lora=False)
    return model.to(device).eval()

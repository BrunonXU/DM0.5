#!/usr/bin/env python3
"""Paired visual-augmentation robustness evaluation for NewLora checkpoints."""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path

import albumentations as A
import cv2
import numpy as np
import torch
from transformers import AutoProcessor

from opendm.data.augmentations import PadToSquare, TransformPipeline
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
from g2_sort.validation.newlora_common import (
    ACTION_DIM,
    ACTION_HORIZON,
    GROUPS,
    NewLoraValidationDataConfig,
    build_model,
    denormalize_quantile,
    load_norm_stats,
    mixed_to_absolute,
    sha256_file,
    train_norm_path,
    validate_static_contract,
)

CONDITIONS = ("original", "crop_5pct", "rotate_2deg", "color_jitter", "official_full_p05")


class SeededVisualPipeline(TransformPipeline):
    """Exact official operators, split into deterministic paired conditions."""

    def __init__(self, condition: str, seed: int) -> None:
        if condition not in CONDITIONS:
            raise ValueError(f"unknown visual condition: {condition}")
        self.condition = condition
        self.seed = seed
        self.p = 1.0
        self.steps = self.build_steps()
        self.transforms = self.steps
        self.compose = A.Compose(self.steps, seed=seed)

    def build_steps(self):
        steps = [
            PadToSquare(border_mode=cv2.BORDER_CONSTANT, fill=0, fill_mask=0, p=1.0),
            A.Resize(448, 448, p=1.0),
        ]
        if self.condition == "crop_5pct":
            steps.append(A.RandomResizedCrop(size=(448, 448), scale=(0.95, 0.95), ratio=(1.0, 1.0), p=1.0))
        elif self.condition == "rotate_2deg":
            steps.append(A.Rotate(limit=(-2, 2), p=1.0))
        elif self.condition == "color_jitter":
            steps.append(A.ColorJitter(brightness=0.3, contrast=0.4, saturation=0.3, hue=0.0, p=1.0))
        elif self.condition == "official_full_p05":
            steps.extend(
                [
                    A.RandomResizedCrop(size=(448, 448), scale=(0.95, 0.95), ratio=(1.0, 1.0), p=0.5),
                    A.Rotate(limit=(-2, 2), p=0.5),
                    A.ColorJitter(brightness=0.3, contrast=0.4, saturation=0.3, hue=0.0, p=0.5),
                ]
            )
        return steps


@dataclass
class VisualValidationDataConfig(NewLoraValidationDataConfig):
    visual_condition: str = field(default="original")
    visual_seed: int = field(default=20260822)

    def build_dataset(self, processor, action_horizon: int, tokenizer_max_length: int = 1024):
        dataset_info = self._dataset_info()
        image_keys = dataset_info["image_keys"]
        norm_path = train_norm_path(self.norm_stats_root)
        pipeline = Pipeline(
            [
                self._action_transform(action_horizon),
                LoadImages(image_keys=image_keys, image_dir=dataset_info["image_dir"]),
                PixelTransform(
                    transform_pipeline=SeededVisualPipeline(self.visual_condition, self.visual_seed),
                    image_keys=image_keys,
                ),
                Normalize(norm_stats_path=str(norm_path), norm_keys=["state", "action"], use_quantiles=True),
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


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--processor", required=True)
    parser.add_argument("--norm-stats-root", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--seed-repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260822)
    parser.add_argument("--diffusion-steps", type=int, default=10)
    parser.add_argument("--max-episodes", type=int)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    validate_static_contract()
    if min(args.batch_size, args.seed_repeats, args.diffusion_steps) < 1:
        raise ValueError("batch-size, seed-repeats and diffusion-steps must be positive")
    device = torch.device(args.device)
    if device.type != "cuda" or not torch.cuda.is_available():
        raise RuntimeError("visual robustness evaluation requires CUDA")

    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    episode_groups = manifest["episodes"]
    if args.max_episodes is not None:
        episode_groups = episode_groups[: args.max_episodes]
    norm_path, stats = load_norm_stats(args.norm_stats_root)
    processor = AutoProcessor.from_pretrained(args.processor, local_files_only=True, trust_remote_code=True)

    datasets = {}
    collators = {}
    for offset, condition in enumerate(CONDITIONS):
        config = VisualValidationDataConfig(
            norm_stats_root=args.norm_stats_root,
            visual_condition=condition,
            visual_seed=args.seed + offset * 100_000,
        )
        datasets[condition], collators[condition] = config.build_dataset(
            processor=processor,
            action_horizon=ACTION_HORIZON,
            tokenizer_max_length=1024,
        )
    reference_dataset = datasets["original"]
    if any(dataset.sample_index != reference_dataset.sample_index for dataset in datasets.values()):
        raise RuntimeError("condition datasets do not share an identical sample index")

    raw_line_cache: dict[str, list[str]] = {}

    def physical_states(indices):
        values = []
        for index in indices:
            file_id, frame_index = reference_dataset.sample_index[index]
            jsonl_path = reference_dataset.id_to_jsonl[file_id]
            if jsonl_path not in raw_line_cache:
                raw_line_cache[jsonl_path] = Path(jsonl_path).read_text(encoding="utf-8").splitlines()
            values.append(json.loads(raw_line_cache[jsonl_path][frame_index])["state"])
        return np.asarray(values, dtype=np.float32)

    torch.manual_seed(args.seed)
    model = build_model(args.model, device)
    episode_records = []

    with torch.inference_mode():
        for episode_offset, group in enumerate(episode_groups):
            condition_values = {condition: {} for condition in CONDITIONS}

            def add(condition, name, value):
                condition_values[condition].setdefault(name, []).append(float(value))

            for batch_offset, start in enumerate(range(0, len(group["indices"]), args.batch_size)):
                indices = group["indices"][start : start + args.batch_size]
                state = physical_states(indices)
                batches = {
                    condition: {
                        key: value.to(device)
                        for key, value in collators[condition]([datasets[condition][i] for i in indices]).items()
                    }
                    for condition in CONDITIONS
                }
                original_seed0 = None
                for repeat in range(args.seed_repeats):
                    generation_seed = args.seed + repeat * 1_000_000 + episode_offset * 10_000 + batch_offset
                    predictions = {}
                    for condition in CONDITIONS:
                        batch = batches[condition]
                        torch.manual_seed(generation_seed)
                        pred_norm = model.inference_action(
                            input_ids=batch["input_ids"],
                            attention_mask=batch["attention_mask"],
                            pixel_values=batch["pixel_values"],
                            token_type_ids=batch["token_type_ids"],
                            diffusion_steps=args.diffusion_steps,
                            action_mask=batch["action_mask"],
                        )[:, :, :ACTION_DIM].float().cpu().numpy()
                        if pred_norm.shape[1:] != (ACTION_HORIZON, ACTION_DIM) or not np.isfinite(pred_norm).all():
                            raise RuntimeError(f"invalid prediction for {group['episode']} {condition}")
                        pred_abs = mixed_to_absolute(denormalize_quantile(pred_norm, stats["action"]), state)
                        predictions[condition] = (pred_norm, pred_abs)

                    original_norm, original_abs = predictions["original"]
                    if repeat == 0:
                        original_seed0 = original_abs.copy()
                    else:
                        add("original", "seed_dispersion_mae", np.abs(original_abs - original_seed0).mean())

                    target_norm = batches["original"]["action"][:, :, :ACTION_DIM].float().cpu().numpy()
                    target_abs = mixed_to_absolute(denormalize_quantile(target_norm, stats["action"]), state)
                    original_pixels = batches["original"]["pixel_values"].float()

                    for condition, (pred_norm, pred_abs) in predictions.items():
                        for group_name, group_slice in GROUPS.items():
                            add(condition, f"physical_mae_{group_name}", np.abs(pred_abs[..., group_slice] - target_abs[..., group_slice]).mean())
                        add(condition, "first_action_jump_mae", np.abs(pred_abs[:, 0] - state).mean())
                        add(condition, "first_action_jump_max", np.abs(pred_abs[:, 0] - state).max())
                        add(condition, "out_of_train_q01_q99_fraction", (np.abs(pred_norm) > 1.0).mean())
                        if condition == "original":
                            continue
                        delta = np.abs(pred_abs - original_abs)
                        add(condition, "visual_delta_mae_all", delta.mean())
                        add(condition, "visual_delta_max", delta.max())
                        add(condition, "visual_delta_mae_arm", delta[..., :14].mean())
                        add(condition, "visual_delta_mae_hand", delta[..., 14:].mean())
                        add(condition, "visual_delta_mae_h1", delta[:, 0].mean())
                        add(condition, "visual_delta_mae_h20", delta[:, :20].mean())
                        pixel_delta = torch.abs(batches[condition]["pixel_values"].float() - original_pixels).mean().item()
                        add(condition, "pixel_tensor_delta_mae", pixel_delta)
                        arm_original = original_abs[:, :20, :14] - state[:, None, :14]
                        arm_condition = pred_abs[:, :20, :14] - state[:, None, :14]
                        numerator = (arm_original * arm_condition).sum(axis=-1)
                        denominator = np.linalg.norm(arm_original, axis=-1) * np.linalg.norm(arm_condition, axis=-1)
                        cosine = numerator / np.maximum(denominator, 1e-8)
                        add(condition, "arm_reach_cosine_h20", cosine.mean())

            episode_conditions = {
                condition: {name: statistics.mean(values) for name, values in metrics.items()}
                for condition, metrics in condition_values.items()
            }
            episode_records.append({"episode": group["episode"], "conditions": episode_conditions})
            print(
                f"episode={group['episode']} "
                f"original_mae={episode_conditions['original']['physical_mae_all']:.6f} "
                f"official_delta={episode_conditions['official_full_p05']['visual_delta_mae_all']:.6f}"
            )

    macro = {}
    for condition in CONDITIONS:
        metric_names = sorted({name for record in episode_records for name in record["conditions"][condition]})
        macro[condition] = {
            name: statistics.mean(record["conditions"][condition][name] for record in episode_records)
            for name in metric_names
        }
        if condition != "original":
            seed_dispersion = macro["original"].get("seed_dispersion_mae", float("nan"))
            macro[condition]["visual_delta_to_seed_dispersion_ratio"] = (
                macro[condition]["visual_delta_mae_all"] / seed_dispersion
                if seed_dispersion > 0 else float("inf")
            )

    result = {
        "schema": "NewLora_visual_aug_paired_v1",
        "model": args.model,
        "manifest": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "norm_stats": str(norm_path),
        "norm_stats_sha256": sha256_file(norm_path),
        "conditions": list(CONDITIONS),
        "augmentation_contract": "official_ops_deterministic_paired; three cameras independently sampled",
        "action_contract": "arm_relative_14_hand_absolute_20",
        "diffusion_steps": args.diffusion_steps,
        "seed": args.seed,
        "seed_repeats": args.seed_repeats,
        "batch_size": args.batch_size,
        "num_episodes": len(episode_records),
        "num_unique_samples": sum(len(group["indices"]) for group in episode_groups),
        "condition_macro_metrics": macro,
        "episodes": episode_records,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output_path)
    print(json.dumps({key: value for key, value in result.items() if key != "episodes"}, ensure_ascii=False))


if __name__ == "__main__":
    main()

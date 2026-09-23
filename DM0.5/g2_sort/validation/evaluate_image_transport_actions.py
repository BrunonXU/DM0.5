#!/usr/bin/env python3
"""Fixed-seed action A/B for DM0.5 image transport candidates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch

from g2_sort.training.NewLora_dm05_lora import (
    DM05DataConfig,
    DM05InferenceConfig,
    DM05ModelConfig,
)
from g2_sort.transport.dm05_binary import BinaryRequest, EncodedImage
from g2_sort.validation.evaluate_image_transport import (
    IMAGE_NAMES,
    _encode_preprocessed,
    _legacy_robot_reencode,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--diffusion-steps", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260824)
    parser.add_argument("--seed-repeats", type=int, default=3)
    parser.add_argument("--compile-action-expert", action="store_true")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def _metric(reference: np.ndarray, candidate: np.ndarray) -> dict[str, float]:
    difference = np.abs(candidate.astype(np.float64) - reference.astype(np.float64))
    return {
        "mae_all_rad": float(difference.mean()),
        "max_all_rad": float(difference.max()),
        "mae_arm_rad": float(difference[..., :14].mean()),
        "max_arm_rad": float(difference[..., :14].max()),
        "mae_hand_rad": float(difference[..., 14:].mean()),
        "max_hand_rad": float(difference[..., 14:].max()),
        "first_step_max_rad": float(difference[:, 0].max()),
    }


def main() -> None:
    args = parse_args()
    if args.diffusion_steps < 1 or args.seed_repeats < 1:
        raise ValueError("diffusion steps and seed repeats must be positive")
    device = torch.device(args.device)
    if device.type != "cuda" or not torch.cuda.is_available():
        raise RuntimeError("image transport action A/B requires CUDA")

    preflight = json.loads((args.archive / "preflight.json").read_text())
    image_records = {
        str(record["name"]): record for record in preflight["images"]
    }
    source_images = [
        (args.archive / f"{name}.jpg").read_bytes() for name in IMAGE_NAMES
    ]
    candidates = {
        "raw_jpeg_passthrough": source_images,
        "legacy_decode_reencode_q95": [
            _legacy_robot_reencode(payload) for payload in source_images
        ],
        "robot_resize_448_png": [
            _encode_preprocessed(payload, "PNG") for payload in source_images
        ],
        "robot_resize_448_jpeg_q95": [
            _encode_preprocessed(payload, "JPEG") for payload in source_images
        ],
    }
    candidate_media_types = {
        "raw_jpeg_passthrough": ["image/jpeg"] * len(IMAGE_NAMES),
        "legacy_decode_reencode_q95": ["image/jpeg"] * len(IMAGE_NAMES),
        "robot_resize_448_png": ["image/png"] * len(IMAGE_NAMES),
        "robot_resize_448_jpeg_q95": ["image/jpeg"] * len(IMAGE_NAMES),
    }
    if all(image_records[name].get("native_filename") for name in IMAGE_NAMES):
        candidates["native_gdk_passthrough"] = [
            (args.archive / image_records[name]["native_filename"]).read_bytes()
            for name in IMAGE_NAMES
        ]
        candidate_media_types["native_gdk_passthrough"] = [
            str(image_records[name]["native_media_type"])
            for name in IMAGE_NAMES
        ]
    state = np.asarray(preflight["state"]["values"], dtype=np.float32)

    model_config = DM05ModelConfig(model_name_or_path=args.model)
    model = model_config.build_model(use_lora=True)
    data_config = DM05DataConfig()
    norm_path = Path(args.model) / "norm_stats.json"
    if not norm_path.is_file():
        norm_path = data_config.norm_stats_path(model_config.chunk_size)
    inference = DM05InferenceConfig(
        diffusion_steps=args.diffusion_steps,
        compile_action_expert=args.compile_action_expert,
    )
    inference._initialize(
        model=model,
        model_name_or_path=args.model,
        norm_stats_path=str(norm_path),
        n_bins=data_config.n_bins,
        model_max_length=1024,
        use_absolute_action=True,
        add_state=True,
    )

    predictions: dict[str, list[np.ndarray]] = {name: [] for name in candidates}
    with torch.inference_mode():
        for repeat in range(args.seed_repeats):
            seed = args.seed + repeat
            for name, payloads in candidates.items():
                request = BinaryRequest(
                    request_id=repeat,
                    state=state,
                    images=tuple(
                        EncodedImage(payload, media_type)
                        for payload, media_type in zip(
                            payloads, candidate_media_types[name], strict=True
                        )
                    ),  # type: ignore[arg-type]
                )
                data = inference._prepare_binary_input(request)
                torch.manual_seed(seed)
                actions = inference._predict(data)
                predictions[name].append(np.asarray(actions, dtype=np.float64))

    reference = np.stack(predictions["raw_jpeg_passthrough"])
    result = {
        "schema": "dm05_image_transport_action_ab_v1",
        "model": args.model,
        "archive": str(args.archive),
        "diffusion_steps": args.diffusion_steps,
        "compile_action_expert": args.compile_action_expert,
        "seeds": [args.seed + index for index in range(args.seed_repeats)],
        "reference": "raw_jpeg_passthrough",
        "candidate_vs_same_seed_raw": {
            name: _metric(reference, np.stack(values))
            for name, values in predictions.items()
        },
        "raw_seed_dispersion": (
            None
            if args.seed_repeats < 2
            else _metric(reference[:1], reference[1:])
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

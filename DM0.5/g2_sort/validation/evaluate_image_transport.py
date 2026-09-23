#!/usr/bin/env python3
"""Compare legacy and candidate G2 image transport paths at model-pixel level.

This is intentionally an image-only gate.  It answers whether a transport
candidate produces the same 448x448 RGB tensor input; action quality still
requires a fixed-seed model A/B after this gate passes.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

IMAGE_NAMES = ("head", "left_wrist", "right_wrist")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--iterations", type=int, default=30)
    return parser.parse_args()


def _pil_rgb(payload: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(payload)) as opened:
        return np.asarray(opened.convert("RGB"), dtype=np.uint8)


def _legacy_robot_reencode(payload: bytes) -> bytes:
    bgr = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError("OpenCV could not decode source JPEG")
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    output = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(output, format="JPEG", quality=95)
    return output.getvalue()


def _model_pixels(rgb: np.ndarray) -> np.ndarray:
    # This is the literal implementation frozen in
    # opendm.data.augmentations.NoAugmentationPipeline: symmetric constant-zero
    # PadToSquare followed by Albumentations Resize(448, 448), whose default
    # image interpolation is cv2.INTER_LINEAR.
    height, width = rgb.shape[:2]
    size = max(height, width)
    top = (size - height) // 2
    bottom = size - height - top
    left = (size - width) // 2
    right = size - width - left
    square = cv2.copyMakeBorder(
        rgb,
        top,
        bottom,
        left,
        right,
        borderType=cv2.BORDER_CONSTANT,
        value=0,
    )
    transformed = cv2.resize(square, (448, 448), interpolation=cv2.INTER_LINEAR)
    return np.ascontiguousarray(transformed, dtype=np.uint8)


def _encode_preprocessed(payload: bytes, image_format: str) -> bytes:
    preprocessed = _model_pixels(_pil_rgb(payload))
    output = io.BytesIO()
    kwargs = {"quality": 95} if image_format == "JPEG" else {}
    Image.fromarray(preprocessed, mode="RGB").save(
        output, format=image_format, **kwargs
    )
    return output.getvalue()


def _server_pixels(payload: bytes) -> np.ndarray:
    # The current server decodes with Pillow, then always executes the frozen
    # NoAugmentationPipeline.  A 448x448 candidate therefore tests whether the
    # second pad/resize is an identity in the installed stack.
    return _model_pixels(_pil_rgb(payload))


def _difference(
    reference: np.ndarray, candidate: np.ndarray
) -> dict[str, float | bool | None]:
    delta = candidate.astype(np.int16) - reference.astype(np.int16)
    abs_delta = np.abs(delta)
    mse = float(np.square(delta.astype(np.float64)).mean())
    return {
        "mae_uint8": float(abs_delta.mean()),
        "rmse_uint8": math.sqrt(mse),
        "max_abs_uint8": float(abs_delta.max()),
        "different_value_fraction": float(np.count_nonzero(delta) / delta.size),
        "psnr_db": None
        if mse == 0
        else 20.0 * math.log10(255.0 / math.sqrt(mse)),
        "exact": bool(mse == 0),
    }


def _timed(fn, payload: bytes, iterations: int) -> dict[str, float]:
    elapsed = []
    for _ in range(iterations):
        started = time.perf_counter()
        fn(payload)
        elapsed.append(time.perf_counter() - started)
    values = np.asarray(elapsed, dtype=np.float64)
    return {
        "mean": float(values.mean()),
        "p50": float(np.percentile(values, 50)),
        "p95": float(np.percentile(values, 95)),
        "max": float(values.max()),
    }


def main() -> None:
    args = parse_args()
    if args.iterations < 1:
        raise ValueError("iterations must be positive")

    preflight_path = args.archive / "preflight.json"
    preflight = (
        json.loads(preflight_path.read_text(encoding="utf-8"))
        if preflight_path.is_file()
        else {}
    )
    image_records = preflight.get("images", [])
    image_records_by_name = {
        str(record.get("name")): record for record in image_records
    }
    exact_production_http_archive = bool(image_records) and all(
        record.get("wire_encoder")
        == "g2_client.encode_rgb_jpeg_quality95"
        for record in image_records
    )

    records = []
    for name in IMAGE_NAMES:
        source_path = args.archive / f"{name}.jpg"
        source = source_path.read_bytes()
        reference = _server_pixels(source)
        legacy = _legacy_robot_reencode(source)
        resized_png = _encode_preprocessed(source, "PNG")
        resized_jpeg = _encode_preprocessed(source, "JPEG")
        candidates = {
            "raw_jpeg_passthrough": source,
            "legacy_decode_reencode_q95": legacy,
            "robot_resize_448_png": resized_png,
            "robot_resize_448_jpeg_q95": resized_jpeg,
        }
        native_filename = image_records_by_name.get(name, {}).get(
            "native_filename"
        )
        if native_filename:
            candidates["native_gdk_passthrough"] = (
                args.archive / str(native_filename)
            ).read_bytes()
        zero_stage = {
            "mean": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "max": 0.0,
        }
        robot_stage_seconds = {
            "legacy_decode_reencode_q95": _timed(
                _legacy_robot_reencode, source, args.iterations
            ),
            "raw_jpeg_passthrough": zero_stage,
            "robot_resize_448_png": _timed(
                lambda item: _encode_preprocessed(item, "PNG"),
                source,
                args.iterations,
            ),
            "robot_resize_448_jpeg_q95": _timed(
                lambda item: _encode_preprocessed(item, "JPEG"),
                source,
                args.iterations,
            ),
        }
        if "native_gdk_passthrough" in candidates:
            robot_stage_seconds["native_gdk_passthrough"] = zero_stage
        records.append(
            {
                "image": name,
                "source": str(source_path),
                "source_shape": list(_pil_rgb(source).shape),
                "candidates": {
                    candidate_name: {
                        "bytes": len(candidate),
                        "bytes_vs_source_ratio": len(candidate) / len(source),
                        "model_pixel_difference_vs_raw_passthrough": _difference(
                            reference, _server_pixels(candidate)
                        ),
                    }
                    for candidate_name, candidate in candidates.items()
                },
                "robot_stage_seconds": robot_stage_seconds,
            }
        )

    result = {
        "schema": "dm05_image_transport_equivalence_v1",
        "archive": str(args.archive),
        "iterations": args.iterations,
        "reference": (
            "archived input JPEG decoded by Pillow, then PadToSquare(fill=0) and "
            "Albumentations Resize(448,448)"
        ),
        "archive_contract": preflight.get("image_archive_contract"),
        "exact_production_http_archive": exact_production_http_archive,
        "archive_limitation": (
            "The files are exact multipart HTTP JPEG payloads produced by the "
            "current G2 production encoder from captured RGB arrays. They are "
            "not native compressed GDK payloads used by the binary candidate."
            if exact_production_http_archive
            else (
                "Legacy G2 preflight files were independently saved at JPEG "
                "quality95 and are not proven byte-identical to production "
                "HTTP or native GDK payloads."
            )
        ),
        "claim_boundary": (
            "Pixel equivalence is necessary but not sufficient; run fixed-seed "
            "action A/B before selecting a production transport."
        ),
        "first_principles_constraint": (
            "Arbitrary resize of JPEG pixels requires decode and a new encoding; "
            "raw passthrough and robot-side resize are alternative candidates, "
            "not one operation that can remove both costs."
        ),
        "images": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

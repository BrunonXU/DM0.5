"""Render actual NewLora official-augmentation examples from one G2 episode."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import albumentations as A
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from opendm.data.augmentations import TrainingTransformPipeline


CAMERAS = ("images_head", "images_left_wrist", "images_right_wrist")


def read_video_frame(video_path: Path, frame_index: int) -> np.ndarray:
    capture = cv2.VideoCapture(str(video_path))
    capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
    ok, frame = capture.read()
    capture.release()
    if not ok:
        raise RuntimeError(f"cannot read {video_path} frame {frame_index}")
    return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)


def label(image: np.ndarray, text: str) -> Image.Image:
    pil = Image.fromarray(image)
    canvas = Image.new("RGB", (pil.width, pil.height + 42), "white")
    canvas.paste(pil, (0, 42))
    draw = ImageDraw.Draw(canvas)
    draw.text((8, 12), text, fill="black", font=ImageFont.load_default())
    return canvas


def replay_summary(replay: dict) -> str:
    active = []
    for transform in replay["transforms"]:
        name = transform["__class_fullname__"].split(".")[-1]
        if name in {"PadToSquare", "Resize"} or not transform["applied"]:
            continue
        active.append(name.replace("RandomResizedCrop", "Crop").replace("ColorJitter", "Color"))
    return "+".join(active) if active else "base only"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--row-index", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--variants", type=int, default=4)
    parser.add_argument("--seed", type=int, default=20260814)
    args = parser.parse_args()

    jsonl = args.data_root / "jsonl" / "train" / f"{args.episode}.jsonl"
    rows = [json.loads(line) for line in jsonl.open()]
    row = rows[args.row_index]
    args.output_dir.mkdir(parents=True, exist_ok=True)

    base_steps = TrainingTransformPipeline(p=0.5).steps[:2]
    full_steps = TrainingTransformPipeline(p=0.5).steps
    base = A.Compose(base_steps)
    # Do not inject one shared seed into ReplayCompose: doing so can correlate
    # equal-probability transforms in an illustration, unlike the naturally
    # advancing random streams used by the training pipeline.
    replay = A.ReplayCompose(full_steps)

    rows_out = []
    manifest = {
        "episode": args.episode,
        "row_index": args.row_index,
        "seed": args.seed,
        "cameras": {},
    }
    for camera in CAMERAS:
        item = row[camera]
        frame = read_video_frame(args.data_root / "videos" / item["url"], item["frame_idx"])
        panels = [label(base(image=frame)["image"], f"{camera}: deterministic base")]
        records = []
        for variant in range(1, args.variants + 1):
            result = replay(image=frame)
            summary = replay_summary(result["replay"])
            panels.append(label(result["image"], f"sample {variant}: {summary}"))
            records.append({"variant": variant, "summary": summary})
        sheet = Image.new("RGB", (sum(p.width for p in panels), max(p.height for p in panels)), "white")
        x = 0
        for panel in panels:
            sheet.paste(panel, (x, 0))
            x += panel.width
        output = args.output_dir / f"{camera}_official_aug_examples.jpg"
        sheet.save(output, quality=93)
        rows_out.append(sheet)
        manifest["cameras"][camera] = {"frame_idx": item["frame_idx"], "samples": records}

    combined = Image.new("RGB", (max(r.width for r in rows_out), sum(r.height for r in rows_out)), "white")
    y = 0
    for sheet in rows_out:
        combined.paste(sheet, (0, y))
        y += sheet.height
    combined.save(args.output_dir / "three_camera_official_aug_examples.jpg", quality=93)
    (args.output_dir / "augmentation_replay.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2)
    )


if __name__ == "__main__":
    main()

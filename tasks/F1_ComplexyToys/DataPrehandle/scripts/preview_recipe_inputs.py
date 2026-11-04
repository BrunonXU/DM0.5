#!/usr/bin/env python3
"""Render real V1 pixels: current shared preprocessing vs proposed augmentation.

This is an offline preview only. It does not modify the production processor,
dataset, recipe or model. Requires the existing DM0.5 environment (PIL, NumPy,
Albumentations and OpenCV); no GPU or network is used.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import platform
import sys
from pathlib import Path

import albumentations as A
import cv2
import numpy as np
import PIL
from PIL import Image, ImageDraw, ImageFont

TASK = Path(__file__).resolve().parents[2]
PROJECTS = TASK.parents[1]
sys.path.insert(0, str(TASK / "src"))
from f1_toys.processing import CAMERA_KEYS, crop_box, load_contract, process_image, sha256


def serializable(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(k): serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(v) for v in value]
    return value


def independent_seed(seed, camera, operation):
    key = f"f1-toys-recipe-preview:{seed}:{camera}:{operation}".encode()
    return int.from_bytes(hashlib.sha256(key).digest()[:4], "little")


def build_aug(helper, camera, seed, image_size):
    # Reuse the real helper's operations and defaults in their original order.
    # Current deterministic ROI/PIL preprocessing is retained. The DM helper's
    # own PadToSquare/Resize448 are deliberately not applied a second time.
    source_steps = helper.TrainingTransformPipeline(p=.5).steps[2:]
    if camera != "head":
        source_steps = source_steps[-1:]  # F1 recipe: wrists are color-only.
    steps, seeds = [], {}
    for source in source_steps:
        definition = copy.deepcopy(A.to_dict(source))
        name = type(source).__name__
        if name == "RandomResizedCrop":
            definition["transform"]["size"] = (image_size, image_size)
        steps.append(A.from_dict(definition))
        seeds[name] = independent_seed(seed, camera, name)
    replay = A.ReplayCompose(steps)
    # A shared seed broadcast to all p=.5 steps can correlate their apply gates.
    # Derive independent deterministic streams for every camera and operation.
    for step in replay.transforms:
        step.set_random_seed(seeds[type(step).__name__])
    return replay, seeds


def summary(replay):
    labels = {"RandomResizedCrop": "随机裁剪", "Rotate": "旋转", "ColorJitter": "颜色"}
    applied = [labels[t["__class_fullname__"].split(".")[-1]]
               for t in replay["transforms"] if t["applied"]]
    return " + ".join(applied) if applied else "本 seed 未触发增强"


def font(size, bold=False):
    root = Path("/usr/share/fonts/opentype/noto")
    path = root / ("NotoSansCJK-Bold.ttc" if bold else "NotoSansCJK-Regular.ttc")
    if not path.exists():
        raise RuntimeError("NotoSansCJK fonts required for Chinese preview captions")
    return ImageFont.truetype(str(path), size)


def text(draw, xy, value, size=22, fill="#203044", bold=False):
    draw.text(xy, value, font=font(size, bold), fill=fill, spacing=7)


def fit(image, box, *, pixel_grid=False):
    """Display-only resize; saved individual PNGs retain exact source pixels."""
    width, height = box
    ratio = min(width / image.width, height / image.height)
    size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
    return image.resize(size, Image.Resampling.NEAREST if pixel_grid else Image.Resampling.LANCZOS)


def panel(canvas, xy, image, caption, *, pixel_grid=False, roi=None):
    x, y = xy
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((x, y, x + 416, y + 406), radius=10, fill="white", outline="#d4dce5", width=2)
    shown = fit(image, (384, 328), pixel_grid=pixel_grid)
    if roi is not None:
        scale_x, scale_y = shown.width / image.width, shown.height / image.height
        ImageDraw.Draw(shown).rectangle(tuple(round(v * (scale_x if i % 2 == 0 else scale_y))
                                              for i, v in enumerate(roi)), outline="#f06445", width=3)
    canvas.paste(shown, (x + (416 - shown.width) // 2, y + 12 + (328 - shown.height) // 2))
    text(draw, (x + 12, y + 347), caption, size=19)


def overview(output, record, views, seed):
    canvas = Image.new("RGB", (1812, 1522), "#eef2f7")
    draw = ImageDraw.Draw(canvas)
    text(draw, (28, 17), "V1 真实观测：原图 → ROI → 当前模型输入 → 候选训练增强", 32, bold=True)
    text(draw, (28, 66), f"训练 row {record['dataset_row']}  |  observation {record['observation_id']}  |  t={record['seconds_from_first_anchor']:.3f}s", 22)
    text(draw, (28, 103), "第 3 列已用于当前训练/推理；第 4 列仅为本轮候选预览，尚未接入训练。", 22, fill="#a34c00")
    headers = ["1  原始相机图", "2  固定 ROI / 腕图不裁剪", "3  当前输入 · 224 × 224", f"4  候选增强 · seed {seed}"]
    for col, header in enumerate(headers):
        text(draw, (95 + col * 428, 153), header, 21, bold=True)
    names = {"head": "主图", "left_wrist": "左腕", "right_wrist": "右腕"}
    for row_index, camera in enumerate(CAMERA_KEYS):
        y = 195 + row_index * 420
        text(draw, (15, y + 165), names[camera], 24, bold=True)
        raw, cropped, current, augmented = views[camera]
        frame = record["cameras"][camera]
        captions = [f"{raw.width} × {raw.height}\n红框仅为展示标注" if camera == "head" else f"{raw.width} × {raw.height}\n完整原图",
                    f"{cropped.width} × {cropped.height}\n固定 ROI，无随机变换" if camera == "head" else "不做腕图 ROI\n保留完整视野",
                    "共享 process_image()\n黑边 + 等比例缩放，RGB [-1, 1]",
                    f"{summary(frame['augmentations'][str(seed)]['replay'])}\n224 × 224；训练候选，非推理"]
        for col, img in enumerate((raw, cropped, current, augmented[seed])):
            panel(canvas, (88 + col * 428, y), img, captions[col], pixel_grid=col >= 2,
                  roi=record["head_roi_xyxy_pixels"] if camera == "head" and col == 0 else None)
    text(draw, (28, 1460), "显示放大仅便于观察；pixels/ 中保留未经显示缩放的原生尺寸 PNG。增强不生成新物体或新场景。", 19)
    path = output / f"overview-row-{record['dataset_row']:04d}.png"
    canvas.save(path)
    return path


def seeds_overview(output, record, views, seeds):
    canvas = Image.new("RGB", (1812, 1504), "#eef2f7")
    draw = ImageDraw.Draw(canvas)
    text(draw, (28, 18), "同一观测的训练增强候选：固定 seed，可重现", 32, bold=True)
    text(draw, (28, 68), f"row {record['dataset_row']} / observation {record['observation_id']}；三路分别采样，腕图仅颜色增强。", 23)
    text(draw, (28, 110), "候选尚未接入训练；推理始终只使用第一列的确定性处理。", 22, fill="#a34c00")
    for col, header in enumerate(["当前确定性输入"] + [f"候选 seed {s}" for s in seeds]):
        text(draw, (95 + col * 428, 154), header, 23, bold=True)
    names = {"head": "主图", "left_wrist": "左腕", "right_wrist": "右腕"}
    for r, camera in enumerate(CAMERA_KEYS):
        y = 195 + r * 420
        text(draw, (15, y + 165), names[camera], 24, bold=True)
        panel(canvas, (88, y), views[camera][2], "当前真实 224 × 224\n不做随机增强", pixel_grid=True)
        for col, seed in enumerate(seeds, 1):
            replay = record["cameras"][camera]["augmentations"][str(seed)]["replay"]
            panel(canvas, (88 + col * 428, y), views[camera][3][seed], summary(replay) + "\n固定 seed 的一次采样", pixel_grid=True)
    text(draw, (28, 1461), "主图依次：95% 面积随机裁剪 → ±2°旋转 → 颜色扰动，各 p=0.5；左右腕仅颜色扰动 p=0.5。", 19)
    path = output / f"augmentation-seeds-row-{record['dataset_row']:04d}.png"
    canvas.save(path)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=TASK / "artifacts/20261015-v1-smoke/dataset")
    parser.add_argument("--raw", type=Path, default=TASK / "artifacts/20261015-v1-smoke/raw/f1_hil_20261014T091553Z_88a4ad17")
    parser.add_argument("--output", type=Path, default=TASK / "DataPrehandle/visualizations/20261016-recipe-preview")
    parser.add_argument("--rows", nargs="+", type=int, default=[150, 450, 660])
    parser.add_argument("--seeds", nargs=3, type=int, default=[20261016, 20261017, 20261018])
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Output exists; choose a new output directory to preserve evidence")
    cfg_path = TASK / "configs/roi_right_v1.json"
    cfg = load_contract(cfg_path)
    helper_path = PROJECTS / "DM0.5/opendm/opendm/data/augmentations.py"
    spec = importlib.util.spec_from_file_location("preview_dm_augmentations", helper_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    rows = [json.loads(line) for line in (args.dataset / "samples.jsonl").read_text().splitlines()]
    raw_frames = [json.loads(line) for line in (args.raw / "payload/frames.jsonl").read_text().splitlines()]
    by_observation = {row["observation_id"]: (i, row) for i, row in enumerate(raw_frames)}
    recipe_path = PROJECTS / "F1/foam_sort_whole_body/docs/training/dm05_eef_delta_recipe.md"
    source_files = [cfg_path, args.dataset / "manifest.json", args.dataset / "samples.jsonl",
                    args.raw / "payload/frames.jsonl", helper_path, recipe_path,
                    TASK / "src/f1_toys/processing.py", Path(__file__).resolve()]
    source_hashes = {str(p): sha256(p) for p in source_files}
    args.output.mkdir(parents=True)
    manifest = dict(schema="f1_toys_recipe_image_preview_v1", created_date="2026-10-16", timezone="Asia/Shanghai",
                    current_pipeline="actual shared process_image(); current train_image_augmentation=false",
                    candidate_status="preview only; not wired into production training or inference",
                    source_helper=str(helper_path), source_helper_sha256=sha256(helper_path),
                    source_recipe=str(recipe_path),
                    adaptation="Apply helper steps 2 onward AFTER current deterministic ROI/224; crop output changes 448 to 224; wrists color only",
                    normalization="Images display exact uint8 pixels before x/127.5-1; no display autocontrast",
                    augmentation_parameters=dict(head_order=["RandomResizedCrop", "Rotate", "ColorJitter"], wrist_order=["ColorJitter"],
                        crop_scale=[.95,.95], crop_ratio=[1.,1.], rotate_degrees=[-2,2], color=dict(brightness=.3,contrast=.4,saturation=.3,hue=0), probability_each=.5),
                    dependency_versions=dict(python=platform.python_version(), pillow=PIL.__version__, numpy=np.__version__, albumentations=A.__version__, opencv=cv2.__version__),
                    render_backend="Pillow; matplotlib unavailable in existing environments; no dependency installation",
                    seeds=args.seeds, source_hashes_before=source_hashes, observations=[], outputs={})
    for row_index in args.rows:
        row = rows[row_index]
        raw_index, raw_row = by_observation[row["observation_id"]]
        record = dict(dataset_row=row_index, raw_row=raw_index, episode_id=row["episode_id"], observation_id=row["observation_id"],
                      anchor_monotonic_ns=row["anchor_monotonic_ns"], seconds_from_first_anchor=(row["anchor_monotonic_ns"]-rows[0]["anchor_monotonic_ns"])/1e9,
                      head_roi_xyxy_normalized=cfg["head_roi_xyxy_normalized"], cameras={})
        pixel_dir = args.output / "pixels" / f"row-{row_index:04d}"
        pixel_dir.mkdir(parents=True)
        views = {}
        for camera in CAMERA_KEYS:
            source = args.dataset / row["images"][camera]
            source_hash = sha256(source)
            frame = next(f for f in raw_row["frames"] if f["camera"] == camera)
            if source_hash != frame["payload_sha256"]:
                raise ValueError("Image bytes do not match original recording")
            with Image.open(source) as image:
                raw = image.convert("RGB")
            roi = crop_box(raw.size, cfg) if camera == "head" else (0, 0, raw.width, raw.height)
            cropped = raw.crop(roi)
            current_array = process_image(raw, camera, cfg)
            current = Image.fromarray(current_array)
            if camera == "head":
                record["head_roi_xyxy_pixels"] = list(roi)
            camera_record = dict(source=str(source), source_sha256=source_hash, original_size=list(raw.size), original_frame=frame,
                                 roi_xyxy_pixels=list(roi), model_size=list(current.size), augmentations={})
            for name, image in (("original", raw), ("roi", cropped), ("current-224", current)):
                image.save(pixel_dir / f"{camera}-{name}.png")
            variants = {}
            for seed in args.seeds:
                aug, operation_seeds = build_aug(helper, camera, seed, cfg["image_size"])
                result = aug(image=current_array.copy())
                # Exact replay and repeated seed must reproduce the pixels.
                repeated, _ = build_aug(helper, camera, seed, cfg["image_size"])
                assert np.array_equal(result["image"], repeated(image=current_array.copy())["image"])
                assert np.array_equal(result["image"], A.ReplayCompose.replay(result["replay"], image=current_array.copy())["image"])
                variants[seed] = Image.fromarray(result["image"])
                variants[seed].save(pixel_dir / f"{camera}-candidate-seed-{seed}-224.png")
                camera_record["augmentations"][str(seed)] = dict(operation_seeds=operation_seeds, replay=serializable(result["replay"]),
                                                                 pixels_sha256=hashlib.sha256(result["image"].tobytes()).hexdigest(), seed_repeat_verified=True, replay_verified=True)
            assert np.array_equal(current_array, process_image(raw, camera, cfg))
            assert sha256(source) == source_hash
            record["cameras"][camera] = camera_record
            views[camera] = (raw, cropped, current, variants)
        overview(args.output, record, views, args.seeds[0])
        if row_index == args.rows[len(args.rows)//2]:
            seeds_overview(args.output, record, views, args.seeds)
        manifest["observations"].append(record)
    assert all(sha256(Path(path)) == expected for path, expected in source_hashes.items())
    manifest["sources_unchanged"] = True
    manifest["checks"] = dict(current_processor_repeat=True, independent_seed_repeat=True, replay_pixels_equal=True, source_payload_sha_matches_raw=True)
    for path in sorted(args.output.rglob("*.png")):
        manifest["outputs"][str(path.relative_to(args.output))] = dict(sha256=sha256(path), bytes=path.stat().st_size)
    manifest["script"] = dict(path=str(Path(__file__).resolve()), sha256=sha256(Path(__file__).resolve()))
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(dict(output=str(args.output), pngs=len(manifest["outputs"]), rows=args.rows, checks=manifest["checks"]), ensure_ascii=False))


if __name__ == "__main__":
    main()

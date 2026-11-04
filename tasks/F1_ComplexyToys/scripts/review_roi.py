#!/usr/bin/env python3
"""Render real source frames and the exact model ROI for visual coverage review."""
import argparse
import io
import json
from pathlib import Path
import sys

from PIL import Image, ImageDraw

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "src"))
from f1_toys.processing import crop_box, load_contract, process_image


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--raw", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--contract", type=Path, default=TASK / "configs/roi_right_v1.json")
    p.add_argument("--stride", type=int, default=30)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    cfg = load_contract(args.contract)
    rows = [json.loads(line) for line in (args.raw / "payload/frames.jsonl").read_text().splitlines()]
    origin = rows[0]["available_monotonic_ns"]
    chosen = sorted(set(range(0, len(rows), args.stride)) | {len(rows)-1})
    tiles, details = [], []
    for index in chosen:
        row = rows[index]
        frame = next(f for f in row["frames"] if f["camera"] == "head")
        with (args.raw / "payload" / frame["path"]).open("rb") as stream:
            stream.seek(frame["payload_offset"])
            data = stream.read(frame["payload_bytes"])
        with Image.open(io.BytesIO(data)) as source:
            original = source.convert("RGB")
        box = crop_box(original.size, cfg)
        preview = original.copy()
        ImageDraw.Draw(preview).rectangle(box, outline=(255, 70, 40), width=4)
        preview.thumbnail((384, 216))
        roi = Image.fromarray(process_image(original, "head", cfg))
        tile = Image.new("RGB", (608, 250), (30, 30, 30))
        tile.paste(preview, (0, 24)); tile.paste(roi, (384, 24))
        seconds = (row["available_monotonic_ns"]-origin)/1e9
        ImageDraw.Draw(tile).text((5, 5), f"row {index}   t={seconds:.3f}s   original + model ROI", fill="white")
        tiles.append(tile)
        details.append(dict(source_row=index, observation_id=row["observation_id"], seconds=seconds,
                            head_frame_id=frame["frame_id"], source_sha256=frame["payload_sha256"], crop_box=box))
    for start in range(0, len(tiles), 12):
        sheet = Image.new("RGB", (1216, 1500), (20, 20, 20))
        for offset, tile in enumerate(tiles[start:start+12]):
            sheet.paste(tile, ((offset % 2)*608, (offset // 2)*250))
        sheet.save(args.output / f"contact-{start//12:02d}.jpg", quality=92)
    (args.output / "review_sources.json").write_text(json.dumps(dict(raw=str(args.raw), contract=cfg,
        raw_frames=len(rows), sampled_frames=len(chosen), frames=details,
        coverage_claim="sampled visual evidence only; unobserved future poses are not guaranteed"), indent=2))
    print(json.dumps(dict(output=str(args.output), raw_frames=len(rows), sampled_frames=len(chosen))))


if __name__ == "__main__":
    main()

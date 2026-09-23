"""Draw an acknowledged Sync arm/hand smoothness comparison without matplotlib."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def load_dispatch(path: Path) -> list[dict[str, object]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            event = json.loads(line)
            if event.get("event") == "dispatch_summary" and int(event.get("hand_send_count", 0)) > 0:
                rows.append(event)
    if not rows:
        raise ValueError(f"{path}: no acknowledged arm+hand dispatch summaries")
    return rows


def values(rows: list[dict[str, object]], key: str) -> list[float]:
    return [float(row[key]) for row in rows if row.get(key) is not None]


def percentile(items: list[float], fraction: float) -> float:
    ordered = sorted(items)
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] * (high - position) + ordered[high] * (position - low)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default()


def axes(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], title: str, ylabel: str) -> None:
    x0, y0, x1, y1 = box
    draw.text((x0, y0 - 38), title, fill="#111827", font=font(23, True))
    draw.text((x0, y0 - 10), ylabel, fill="#6b7280", font=font(14))
    draw.line((x0, y0, x0, y1), fill="#6b7280", width=2)
    draw.line((x0, y1, x1, y1), fill="#6b7280", width=2)


def bars(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], data: list[tuple[str, float, str]], log: bool = False) -> None:
    x0, y0, x1, y1 = box
    transformed = [math.log10(max(v, 1e-6)) if log else v for _, v, _ in data]
    floor = min(0.0, min(transformed)) if log else 0.0
    ceiling = max(transformed) * 1.12 or 1.0
    width = (x1 - x0) / len(data)
    for index, ((label, raw, color), value) in enumerate(zip(data, transformed, strict=True)):
        left = x0 + index * width + width * 0.18
        right = x0 + (index + 1) * width - width * 0.18
        top = y1 - (value - floor) / (ceiling - floor) * (y1 - y0)
        draw.rectangle((left, top, right, y1), fill=color)
        text_value = f"{raw:.2f}" if raw < 100 else f"{raw:.0f}"
        draw.text((left, top - 25), text_value, fill="#111827", font=font(15, True))
        draw.multiline_text((left, y1 + 9), label, fill="#374151", font=font(14), align="center", spacing=2)


def lines(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], series: list[tuple[str, list[float], str]], log: bool = False) -> None:
    x0, y0, x1, y1 = box
    all_values = [v for _, vals, _ in series for v in vals]
    transformed_all = [math.log10(max(v, 1e-6)) if log else v for v in all_values]
    low = min(transformed_all) if log else 0.0
    high = max(transformed_all)
    if high == low:
        high += 1.0
    count = max(len(vals) for _, vals, _ in series)
    for label, vals, color in series:
        transformed = [math.log10(max(v, 1e-6)) if log else v for v in vals]
        points = []
        for index, value in enumerate(transformed):
            x = x0 + index / max(count - 1, 1) * (x1 - x0)
            y = y1 - (value - low) / (high - low) * (y1 - y0)
            points.append((x, y))
        if len(points) > 1:
            draw.line(points, fill=color, width=3)
        for point in points:
            draw.ellipse((point[0] - 2, point[1] - 2, point[0] + 2, point[1] + 2), fill=color)
    legend_x = x1 - 180
    for index, (label, _, color) in enumerate(series):
        y = y0 + index * 26
        draw.line((legend_x, y + 9, legend_x + 24, y + 9), fill=color, width=4)
        draw.text((legend_x + 31, y), label, fill="#111827", font=font(15))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    baseline = load_dispatch(args.baseline)
    candidate = load_dispatch(args.candidate)
    colors = {"arm": "#2563eb", "hand": "#dc2626"}
    image = Image.new("RGB", (1500, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.text((55, 24), "Sync smoothness: V1.1 vs shared soft preview", fill="#111827", font=font(31, True))
    draw.text((55, 66), "Descriptive cross-run comparison; model trajectories differ", fill="#6b7280", font=font(18))

    step_box = (80, 160, 680, 430)
    jerk_box = (820, 160, 1420, 430)
    timeline_step = (80, 620, 680, 890)
    timeline_jerk = (820, 620, 1420, 890)
    axes(draw, step_box, "P95 maximum positional step", "degrees (lower is smoother)")
    axes(draw, jerk_box, "P95 maximum jerk", "rad/s^3, log-height bars")
    step_data = []
    jerk_data = []
    for run_label, rows in (("V1.1", baseline), ("Soft preview", candidate)):
        for group in ("arm", "hand"):
            step = percentile(values(rows, f"{group}_max_delta_rad"), 0.95) * 180 / math.pi
            jerk = percentile(values(rows, f"{group}_actual_max_jerk_rad_s3"), 0.95)
            step_data.append((f"{run_label}\n{group}", step, colors[group]))
            jerk_data.append((f"{run_label}\n{group}", jerk, colors[group]))
    bars(draw, step_box, step_data)
    bars(draw, jerk_box, jerk_data, log=True)

    axes(draw, timeline_step, "Latest run: per-chunk positional step", "degrees; chunk index left to right")
    axes(draw, timeline_jerk, "Latest run: per-chunk jerk", "rad/s^3, logarithmic scale")
    step_series = []
    jerk_series = []
    for group in ("arm", "hand"):
        step_series.append((group, [v * 180 / math.pi for v in values(candidate, f"{group}_max_delta_rad")], colors[group]))
        jerk_series.append((group, values(candidate, f"{group}_actual_max_jerk_rad_s3"), colors[group]))
    lines(draw, timeline_step, step_series)
    lines(draw, timeline_jerk, jerk_series, log=True)
    draw.text((55, 957), f"Baseline: {len(baseline)} chunks    Latest: {len(candidate)} chunks", fill="#6b7280", font=font(16))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.output)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Plot the strongest near-stationary arm reversal in a powered Sync log."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from g2_sort.deployment.g2_client.joint_layout import (
    POLICY_JOINT_NAMES,
    clip_action_positions,
)
from g2_sort.deployment.g2_client.trajectory_smoothing import smooth_arm_action_chunk
from g2_sort.deployment.g2_client.servo import (
    project_stationary_arm_minimum_jerk,
)
from g2_sort.validation.evaluate_rtc_servo_ablation import sample


def font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default()


def load(path: Path) -> tuple[dict[int, dict], list[dict]]:
    events = [json.loads(line) for line in path.read_text().splitlines()]
    chunks = {
        int(event["request_id"]): event
        for event in events
        if event.get("event") == "action_chunk"
    }
    dispatches = [
        event
        for event in events
        if event.get("event") == "dispatch_summary"
        and event.get("arm_previous_command_rad") is not None
    ]
    return chunks, dispatches


def reconstruct(chunk: dict, dispatch: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    raw = np.asarray(chunk["actions"], dtype=np.float64)
    clipped, _, _ = clip_action_positions(raw)
    prepared, _ = smooth_arm_action_chunk(clipped, jerk_weight=10.0)
    duration = 20 / 30
    times = np.arange(int(np.ceil(duration * 100)), dtype=np.float64) / 100.0
    times = np.append(times, duration)
    command = sample(prepared[:, :14], times * 30.0, "spline")
    anchor = np.asarray(dispatch["arm_previous_command_rad"], dtype=np.float64)
    active = times < 0.12
    u = times[active] / 0.12
    alpha = u * u * (3.0 - 2.0 * u)
    command[active] = (1.0 - alpha[:, None]) * anchor + alpha[:, None] * command[active]
    return clipped[:, :14], prepared[:, :14], command


def strongest(chunks: dict[int, dict], dispatches: list[dict]) -> tuple[dict, int, np.ndarray, np.ndarray, np.ndarray]:
    best = None
    for dispatch in dispatches:
        raw, prepared, command = reconstruct(chunks[int(dispatch["request_id"])], dispatch)
        projected, active, reversal_count, _ = (
            project_stationary_arm_minimum_jerk(command, 0.0035)
        )
        for joint in active:
            travel = float(np.sum(np.abs(np.diff(command[:, joint]))))
            projected_travel = float(
                np.sum(np.abs(np.diff(projected[:, joint])))
            )
            score = (travel - projected_travel, reversal_count)
            candidate = (score, dispatch, joint, raw, prepared, command)
            if best is None or candidate[0] > best[0]:
                best = candidate
    if best is None:
        raise ValueError("no near-stationary arm segment found")
    _, dispatch, joint, raw, prepared, command = best
    return dispatch, joint, raw, prepared, command


def draw_series(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], series: list[tuple[str, np.ndarray, np.ndarray, str]]) -> None:
    x0, y0, x1, y1 = box
    all_y = np.concatenate([values for _, _, values, _ in series])
    low, high = float(all_y.min()), float(all_y.max())
    margin = max((high - low) * 0.12, 0.05)
    low, high = low - margin, high + margin
    draw.line((x0, y0, x0, y1), fill="#6b7280", width=2)
    draw.line((x0, y1, x1, y1), fill="#6b7280", width=2)
    for label, times, values, color in series:
        points = [
            (
                x0 + float(t) / (2 / 3) * (x1 - x0),
                y1 - (float(v) - low) / (high - low) * (y1 - y0),
            )
            for t, v in zip(times, values, strict=True)
        ]
        draw.line(points, fill=color, width=4)
        for x, y in points:
            draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=color)
    for index, (label, _, _, color) in enumerate(series):
        x, y = x0 + 20 + index * 265, y0 + 16
        draw.line((x, y + 10, x + 32, y + 10), fill=color, width=5)
        draw.text((x + 42, y), label, fill="#111827", font=font(18))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    chunks, dispatches = load(args.log)
    dispatch, joint, raw, prepared, command = strongest(chunks, dispatches)
    request_id = int(dispatch["request_id"])
    raw_y = np.degrees(raw[:21, joint] - command[0, joint])
    prepared_y = np.degrees(prepared[:21, joint] - command[0, joint])
    command_y = np.degrees(command[:, joint] - command[0, joint])
    candidate, _, _, _ = project_stationary_arm_minimum_jerk(command, 0.0035)
    candidate_y = np.degrees(candidate[:, joint] - candidate[0, joint])
    raw_t = np.arange(21) / 30.0
    command_t = np.arange(len(command)) / 100.0
    travel = float(np.sum(np.abs(np.diff(command_y))))
    net = float(abs(command_y[-1] - command_y[0]))
    span = float(command_y.max() - command_y.min())
    candidate_travel = float(np.sum(np.abs(np.diff(candidate_y))))

    image = Image.new("RGB", (1450, 820), "white")
    draw = ImageDraw.Draw(image)
    name = POLICY_JOINT_NAMES[joint]
    draw.text((65, 35), "Powered Sync near-stationary tremor evidence", fill="#111827", font=font(32, True))
    draw.text((65, 85), f"request {request_id} · {name}", fill="#374151", font=font(22))
    draw.text((65, 132), "Position relative to segment start (degrees)", fill="#6b7280", font=font(17))
    draw_series(
        draw,
        (80, 190, 1370, 625),
        [
            ("raw model @30Hz", raw_t, raw_y, "#dc2626"),
            ("weight10 @30Hz", raw_t, prepared_y, "#16a34a"),
            ("C2 target @100Hz", command_t, command_y, "#2563eb"),
            ("3.5mrad candidate", command_t, candidate_y, "#9333ea"),
        ],
    )
    draw.text((80, 650), "0s", fill="#6b7280", font=font(16))
    draw.text((1310, 650), "0.66s", fill="#6b7280", font=font(16))
    draw.text(
        (80, 700),
        f"C2: net {net:.3f}°, travel {travel:.3f}°, range {span:.3f}°; candidate travel {candidate_travel:.3f}°.",
        fill="#111827",
        font=font(22, True),
    )
    draw.text(
        (80, 742),
        "Small net motion with much larger travel is commanded back-and-forth motion, not a network deadline miss.",
        fill="#6b7280",
        font=font(18),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.output)
    print(f"{args.output} request={request_id} joint={name} net={net:.3f} travel={travel:.3f} span={span:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

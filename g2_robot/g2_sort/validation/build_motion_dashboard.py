"""Build a dependency-light static dashboard from G2 JSONL telemetry.

The generator is intentionally offline: it never contacts the robot and never
changes source logs.  NumPy computes metrics and Pillow renders reproducible
PNG figures; the browser only needs static HTML/CSS/JavaScript.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from g2_sort.deployment.g2_client.joint_layout import POLICY_JOINT_NAMES


SCHEMA_VERSION = 1
MODEL_HZ = 30.0
ARM_HZ = 100.0
COLORS = {
    "background": "#f6f8fb",
    "panel": "#ffffff",
    "grid": "#d8dee9",
    "text": "#172033",
    "muted": "#657087",
    "model": "#3468d4",
    "command": "#e08524",
    "measured": "#1b9a72",
    "danger": "#c23b53",
}


@dataclass
class RunData:
    source: Path
    source_relative: str
    source_hash: str
    run_id: str
    records: list[dict[str, Any]]
    malformed_lines: int
    feedback_records: list[dict[str, Any]]


def _font(size: int = 14) -> ImageFont.ImageFont:
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    )
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    malformed = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if isinstance(value, dict):
                records.append(value)
            else:
                malformed += 1
    return records, malformed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run_id(relative: str, digest: str) -> str:
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", Path(relative).stem).strip("-")
    return f"{stem[-72:]}-{digest[:10]}"


def _as_matrix(value: Any, dim: int) -> np.ndarray:
    try:
        array = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError):
        return np.empty((0, dim), dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != dim or not np.all(np.isfinite(array)):
        return np.empty((0, dim), dtype=np.float64)
    return array


def _as_vector(value: Any, dim: int) -> np.ndarray:
    try:
        array = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError):
        return np.empty(0, dtype=np.float64)
    if array.shape != (dim,) or not np.all(np.isfinite(array)):
        return np.empty(0, dtype=np.float64)
    return array


def _f710_samples(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if record.get("event") == "f710_control_sample"
        and isinstance(record.get("control_monotonic_ns"), int)
    ]


def _f710_dispatch_samples(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the exact dispatch timebase, including lightweight A/B rows."""
    return [
        record
        for record in records
        if record.get("event") in {"f710_control_sample", "f710_dispatch_sample"}
        and isinstance(record.get("control_monotonic_ns"), int)
    ]


def _f710_command_trace(
    records: Sequence[dict[str, Any]],
) -> tuple[np.ndarray, np.ndarray]:
    times: list[float] = []
    values: list[np.ndarray] = []
    for record in _f710_dispatch_samples(records):
        row = _as_vector(record.get("command_q_rad"), 34)
        if not len(row):
            arm = _as_vector(record.get("arm_command_rad"), 14)
            if not len(arm):
                continue
            row = np.full(34, np.nan, dtype=np.float64)
            row[:14] = arm
        times.append(int(record["control_monotonic_ns"]) / 1e9)
        values.append(row)
    return (
        (np.asarray(times), np.stack(values))
        if values
        else (np.empty(0), np.empty((0, 34)))
    )


def _f710_trace(
    records: Sequence[dict[str, Any]], key: str, dim: int
) -> tuple[np.ndarray, np.ndarray]:
    times: list[float] = []
    values: list[np.ndarray] = []
    for record in _f710_samples(records):
        row = _as_vector(record.get(key), dim)
        if len(row):
            times.append(int(record["control_monotonic_ns"]) / 1e9)
            values.append(row)
    return (
        (np.asarray(times), np.stack(values))
        if values
        else (np.empty(0), np.empty((0, dim)))
    )


def _f710_ik_arm_trace(
    records: Sequence[dict[str, Any]],
) -> tuple[np.ndarray, np.ndarray]:
    times: list[float] = []
    values: list[np.ndarray] = []
    for record in _f710_samples(records):
        local = _as_vector(record.get("ik_q_ref_rad"), 7)
        side = record.get("active_side")
        if not len(local) or side not in {"left", "right"}:
            continue
        row = np.full(14, np.nan, dtype=np.float64)
        if side == "left":
            row[:7] = local
        else:
            row[7:] = local
        times.append(int(record["control_monotonic_ns"]) / 1e9)
        values.append(row)
    return (
        (np.asarray(times), np.stack(values))
        if values
        else (np.empty(0), np.empty((0, 14)))
    )


def _events(records: Sequence[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [record for record in records if record.get("event") == event]


def _first_number(records: Sequence[dict[str, Any]], *names: str) -> float | None:
    for record in records:
        for name in names:
            value = record.get(name)
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                return float(value)
    return None


def _mode(records: Sequence[dict[str, Any]], source: Path) -> str:
    for record in records:
        value = record.get("mode")
        if isinstance(value, str) and value:
            return value
    name = source.name.lower()
    if "rtc" in name:
        return "powered-rtc"
    if "sync" in name:
        return "powered-sync"
    return "unknown"


def _mode_family(mode: str) -> str:
    lowered = mode.lower()
    if mode == "powered-f710" or "f710" in lowered or "teleop" in lowered:
        return "f710"
    if "rtc" in lowered:
        return "rtc"
    if "sync" in lowered:
        return "sync"
    return "other"


def _started_at(records: Sequence[dict[str, Any]], source: Path) -> float:
    values = [
        float(record["wall_time"])
        for record in records
        if isinstance(record.get("wall_time"), (int, float))
    ]
    return min(values) if values else source.stat().st_mtime


def _profile(records: Sequence[dict[str, Any]], source: Path) -> str:
    starts = _events(records, "runtime_start")
    keys = ("hand_smoothing_profile", "arm_interpolation_profile", "profile")
    parts: list[str] = []
    for key in keys:
        for record in starts:
            value = record.get(key)
            if value not in (None, "", "off"):
                parts.append(str(value))
                break
    if parts:
        return " + ".join(parts)
    match = re.search(r"(c2_spline[^_]*|hand_[a-z0-9_]+)", source.stem)
    return match.group(0) if match else "legacy/unknown"


def _endpoint(records: Sequence[dict[str, Any]]) -> str:
    for record in _events(records, "runtime_start"):
        for key in ("policy_endpoint", "endpoint"):
            if record.get(key):
                return str(record[key])
    return "unknown"


def _committed_model_chunk(record: dict[str, Any]) -> np.ndarray:
    """Return the inclusive waypoint prefix actually committed to the servo."""
    chunk = _as_matrix(record.get("actions"), 34)
    if not len(chunk):
        return np.empty((0, 34), dtype=np.float64)
    start_value = record.get("used_start_index")
    start = int(start_value) if isinstance(start_value, int) else 0
    interval_value = record.get("used_intervals")
    intervals = (
        int(interval_value)
        if isinstance(interval_value, int)
        else len(chunk) - start - 1
    )
    start = min(max(start, 0), len(chunk) - 1)
    end = min(start + max(intervals, 0), len(chunk) - 1)
    return chunk[start : end + 1]


def _model_chunks(records: Sequence[dict[str, Any]]) -> list[np.ndarray]:
    """Return only model waypoints committed by the scheduler."""
    chunks: list[np.ndarray] = []
    for record in _events(records, "action_chunk"):
        chunk = _committed_model_chunk(record)
        if len(chunk):
            chunks.append(chunk)
    return chunks


def _action_by_request(records: Sequence[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {
        int(record["request_id"]): record
        for record in _events(records, "action_chunk")
        if isinstance(record.get("request_id"), int)
    }


def _model_trace(records: Sequence[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray]:
    times: list[np.ndarray] = []
    values: list[np.ndarray] = []
    fallback = 0.0
    dispatch = _dispatch_by_request(records)
    for record in _events(records, "action_chunk"):
        chunk = _committed_model_chunk(record)
        if not len(chunk):
            continue
        base_value = record.get("response_received_at_monotonic")
        request_id = record.get("request_id")
        if not isinstance(base_value, (int, float)) and isinstance(request_id, int):
            base_value = dispatch.get(request_id, {}).get("segment_started_at_monotonic")
        base = float(base_value) if isinstance(base_value, (int, float)) else fallback
        segment_times = base + np.arange(len(chunk), dtype=np.float64) / MODEL_HZ
        times.append(segment_times)
        values.append(chunk)
        fallback = float(segment_times[-1] + 1.0 / MODEL_HZ)
    if not values:
        return np.empty(0), np.empty((0, 34))
    return np.concatenate(times), np.concatenate(values, axis=0)


def _dispatch_by_request(records: Sequence[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for record in _events(records, "dispatch_summary"):
        request_id = record.get("request_id")
        if isinstance(request_id, int):
            result[request_id] = record
    return result


def _command_chunk(dispatch: dict[str, Any]) -> np.ndarray:
    arm = _as_matrix(dispatch.get("arm_commands_rad"), 14)
    hand = _as_matrix(dispatch.get("hand_commands_rad"), 20)
    if not len(arm) and not len(hand):
        return np.empty((0, 34), dtype=np.float64)
    count = max(len(arm), len(hand))
    output = np.full((count, 34), np.nan, dtype=np.float64)
    if len(arm):
        indices = np.minimum(
            (np.arange(count) * len(arm) / count).astype(int), len(arm) - 1
        )
        output[:, :14] = arm[indices]
    if len(hand):
        indices = np.minimum(
            (np.arange(count) * len(hand) / count).astype(int), len(hand) - 1
        )
        output[:, 14:] = hand[indices]
    return output


def _command_traces(
    records: Sequence[dict[str, Any]],
) -> tuple[tuple[np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]]:
    actions = _action_by_request(records)
    arm_times: list[np.ndarray] = []
    arm_values: list[np.ndarray] = []
    hand_times: list[np.ndarray] = []
    hand_values: list[np.ndarray] = []
    for request_id, item in sorted(_dispatch_by_request(records).items()):
        base_value = item.get("segment_started_at_monotonic")
        if not isinstance(base_value, (int, float)):
            base_value = actions.get(request_id, {}).get(
                "response_received_at_monotonic"
            )
        if not isinstance(base_value, (int, float)):
            continue
        base = float(base_value)
        streams = (
            (
                14,
                "arm_commands_rad",
                "arm_command_time_offsets_seconds",
                arm_times,
                arm_values,
            ),
            (
                20,
                "hand_commands_rad",
                "hand_command_time_offsets_seconds",
                hand_times,
                hand_values,
            ),
        )
        for dim, values_key, offsets_key, out_times, out_values in streams:
            values = _as_matrix(item.get(values_key), dim)
            offsets = np.asarray(item.get(offsets_key, []), dtype=np.float64)
            if (
                len(values)
                and len(values) == len(offsets)
                and np.all(np.isfinite(offsets))
            ):
                out_times.append(base + offsets)
                out_values.append(values)
    arm = (
        (np.concatenate(arm_times), np.concatenate(arm_values, axis=0))
        if arm_values
        else (np.empty(0), np.empty((0, 14)))
    )
    hand = (
        (np.concatenate(hand_times), np.concatenate(hand_values, axis=0))
        if hand_values
        else (np.empty(0), np.empty((0, 20)))
    )
    return arm, hand


def _activity(chunks: Sequence[np.ndarray]) -> np.ndarray:
    if not chunks:
        return np.empty((34, 0), dtype=np.float64)
    columns: list[np.ndarray] = []
    for chunk in chunks:
        if len(chunk) < 2:
            columns.append(np.zeros(34, dtype=np.float64))
        else:
            delta = np.abs(np.diff(chunk, axis=0))
            column = np.zeros(34, dtype=np.float64)
            for joint in range(34):
                finite = delta[:, joint][np.isfinite(delta[:, joint])]
                column[joint] = float(np.mean(finite)) if len(finite) else 0.0
            columns.append(column)
    return np.stack(columns, axis=1)


def _metrics(chunks: Sequence[np.ndarray], hz: float) -> list[dict[str, Any]]:
    valid_chunks = [chunk for chunk in chunks if chunk.ndim == 2 and chunk.shape[1] == 34 and len(chunk)]
    if not valid_chunks:
        return []
    sequence = np.concatenate(valid_chunks, axis=0)
    results: list[dict[str, Any]] = []
    for index, name in enumerate(POLICY_JOINT_NAMES):
        values = sequence[:, index]
        values = values[np.isfinite(values)]
        if len(values) < 2:
            continue
        deltas: list[np.ndarray] = []
        jerks: list[np.ndarray] = []
        reversals = 0
        for chunk in valid_chunks:
            chunk_values = chunk[:, index]
            chunk_values = chunk_values[np.isfinite(chunk_values)]
            if len(chunk_values) < 2:
                continue
            delta = np.diff(chunk_values)
            deltas.append(delta)
            # Ignore sub-0.1 mrad numerical chatter while retaining physical micro-motion.
            nonzero = delta[np.abs(delta) > 1e-4]
            if len(nonzero) > 1:
                reversals += int(np.sum(nonzero[1:] * nonzero[:-1] < 0))
            if len(chunk_values) >= 4:
                jerks.append(np.diff(chunk_values, n=3) * hz**3)
        delta = np.concatenate(deltas) if deltas else np.empty(0)
        jerk = np.concatenate(jerks) if jerks else np.empty(0)
        net = float(abs(values[-1] - values[0]))
        travel = float(np.sum(np.abs(delta)))
        results.append(
            {
                "index": index,
                "joint": name,
                "net_rad": net,
                "travel_rad": travel,
                "travel_ratio": travel / max(net, 1e-6),
                "reversals": reversals,
                "step_p95_rad": float(np.percentile(np.abs(delta), 95)) if len(delta) else 0.0,
                "max_jerk_rad_s3": float(np.max(np.abs(jerk))) if len(jerk) else None,
                "score": travel + 0.002 * reversals,
            }
        )
    return sorted(results, key=lambda row: float(row["score"]), reverse=True)


def _canvas(title: str, width: int, height: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (width, height), COLORS["background"])
    draw = ImageDraw.Draw(image)
    draw.rectangle((15, 15, width - 15, height - 15), fill=COLORS["panel"], outline=COLORS["grid"])
    draw.text((30, 25), title, fill=COLORS["text"], font=_font(20))
    return image, draw


def _save_heatmap(
    layers: Sequence[tuple[str, np.ndarray]],
    path: Path,
    *,
    title: str = "34DoF activity heatmap (mean |delta q| per chunk)",
) -> bool:
    layers = [(name, data) for name, data in layers if data.size]
    if not layers:
        return False
    width = 1280
    row_height = 250
    height = 80 + row_height * len(layers)
    image, draw = _canvas(title, width, height)
    left, right = 145, width - 45
    cmap = [(248, 250, 253), (90, 124, 210), (194, 59, 83)]
    for layer_index, (name, data) in enumerate(layers):
        top = 70 + layer_index * row_height
        draw.text((30, top + 80), name.upper(), fill=COLORS.get(name, COLORS["text"]), font=_font(16))
        positive = data[np.isfinite(data) & (data > 0)]
        scale = float(np.percentile(positive, 95)) if positive.size else 1.0
        cell_w = max(1.0, (right - left) / max(1, data.shape[1]))
        cell_h = 5.2
        for joint in range(34):
            for column in range(data.shape[1]):
                value = float(data[joint, column]) if np.isfinite(data[joint, column]) else 0.0
                ratio = min(1.0, value / max(scale, 1e-12))
                if ratio < 0.5:
                    blend = ratio * 2
                    color = tuple(int(cmap[0][i] * (1 - blend) + cmap[1][i] * blend) for i in range(3))
                else:
                    blend = (ratio - 0.5) * 2
                    color = tuple(int(cmap[1][i] * (1 - blend) + cmap[2][i] * blend) for i in range(3))
                x0 = int(left + column * cell_w)
                y0 = int(top + joint * cell_h)
                draw.rectangle((x0, y0, int(x0 + cell_w + 1), int(y0 + cell_h + 1)), fill=color)
        for boundary in (7, 14, 24):
            y = int(top + boundary * cell_h)
            draw.line((left, y, right, y), fill="#20293a", width=1)
        draw.text((left, top + 185), "L-arm   R-arm                 L-hand                         R-hand", fill=COLORS["muted"], font=_font(12))
        draw.text((right - 180, top + 205), f"P95 scale={scale:.3g} rad", fill=COLORS["muted"], font=_font(12))
    image.save(path)
    return True


def _feedback_trace(records: Sequence[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray]:
    times: list[float] = []
    values: list[np.ndarray] = []
    for record in records:
        stamp = record.get("read_completed_monotonic_ns")
        row = np.asarray(record.get("joints_rad", []), dtype=np.float64)
        if isinstance(stamp, int) and row.shape == (34,) and np.all(np.isfinite(row)):
            times.append(stamp / 1e9)
            values.append(row)
    return (
        (np.asarray(times), np.stack(values))
        if values
        else (np.empty(0), np.empty((0, 34)))
    )


def _line_segments(
    times: np.ndarray,
    values: np.ndarray,
    box: tuple[int, int, int, int],
    x_range: tuple[float, float],
    y_range: tuple[float, float],
) -> list[list[tuple[float, float]]]:
    valid = np.isfinite(times) & np.isfinite(values)
    times = times[valid]
    values = values[valid]
    if not len(times):
        return []
    order = np.argsort(times, kind="stable")
    times, values = times[order], values[order]
    x_low, x_high = x_range
    y_low, y_high = y_range
    if x_high <= x_low or y_high <= y_low:
        return []
    gaps = np.diff(times)
    positive = gaps[gaps > 0]
    split_threshold = max(float(np.median(positive)) * 2.5, 0.05) if len(positive) else math.inf
    split_at = list(np.flatnonzero(gaps > split_threshold) + 1)
    output: list[list[tuple[float, float]]] = []
    for segment_times, segment_values in zip(
        np.split(times, split_at), np.split(values, split_at), strict=True
    ):
        points = [
            (
                box[0] + (float(stamp) - x_low) * (box[2] - box[0]) / (x_high - x_low),
                box[3] - (float(value) - y_low) * (box[3] - box[1]) / (y_high - y_low),
            )
            for stamp, value in zip(segment_times, segment_values, strict=True)
        ]
        if points:
            output.append(points)
    return output


def _balanced_top_four(ranking: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    arm = [row for row in ranking if int(row["index"]) < 14][:2]
    hand = [row for row in ranking if int(row["index"]) >= 14][:2]
    selected = arm + hand
    for row in ranking:
        if len(selected) >= 4:
            break
        if row not in selected:
            selected.append(row)
    return selected


def _save_top_joints(
    model_trace: tuple[np.ndarray, np.ndarray],
    command_traces: tuple[tuple[np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]],
    measured_trace: tuple[np.ndarray, np.ndarray],
    ranking: Sequence[dict[str, Any]],
    path: Path,
) -> bool:
    model_times, model = model_trace
    if not ranking or not len(model):
        return False
    selected = _balanced_top_four(ranking)
    image, draw = _canvas("Top arm/hand trajectories (real monotonic run time)", 1280, 190 + 220 * len(selected))
    legend = (("model", COLORS["model"]), ("command", COLORS["command"]), ("measured", COLORS["measured"]))
    for i, (label, color) in enumerate(legend):
        draw.line((870 + i * 125, 42, 900 + i * 125, 42), fill=color, width=3)
        draw.text((906 + i * 125, 33), label, fill=color, font=_font(13))
    for row, metric in enumerate(selected):
        index = int(metric["index"])
        top = 80 + row * 220
        box = (225, top + 20, 1225, top + 175)
        draw.rectangle(box, outline=COLORS["grid"])
        draw.text((30, top + 20), str(metric["joint"]), fill=COLORS["text"], font=_font(13))
        draw.text((30, top + 50), f"travel={metric['travel_rad']:.4f} rad", fill=COLORS["muted"], font=_font(12))
        draw.text((30, top + 72), f"reversals={metric['reversals']}", fill=COLORS["muted"], font=_font(12))
        arm_trace, hand_trace = command_traces
        command_times, command_values = arm_trace if index < 14 else hand_trace
        command_index = index if index < 14 else index - 14
        measured_times, measured = measured_trace
        layers = [
            (model_times, model[:, index], COLORS["model"]),
            (
                command_times,
                command_values[:, command_index] if len(command_values) else np.empty(0),
                COLORS["command"],
            ),
            (
                measured_times,
                measured[:, index] if len(measured) else np.empty(0),
                COLORS["measured"],
            ),
        ]
        valid_times = np.concatenate([times[np.isfinite(times)] for times, values, _ in layers if len(times) and len(values)])
        valid_values = np.concatenate([values[np.isfinite(values)] for times, values, _ in layers if len(times) and len(values)])
        if not len(valid_times) or not len(valid_values):
            continue
        x_low, x_high = float(np.min(valid_times)), float(np.max(valid_times))
        y_low, y_high = float(np.min(valid_values)), float(np.max(valid_values))
        y_margin = max((y_high - y_low) * 0.1, 1e-4)
        y_range = (y_low - y_margin, y_high + y_margin)
        x_range = (x_low, max(x_high, x_low + 1e-6))
        for times, values, color in layers:
            for points in _line_segments(times, values, box, x_range, y_range):
                if len(points) >= 2:
                    draw.line(points, fill=color, width=2)
        draw.text((box[0], box[3] + 5), f"t=0 … {x_high - x_low:.2f}s; shared y={y_range[0]:.3f} … {y_range[1]:.3f} rad", fill=COLORS["muted"], font=_font(10))
    image.save(path)
    return True


def _save_timing(records: Sequence[dict[str, Any]], path: Path) -> bool:
    chunks = _events(records, "action_chunk")
    dispatch = _events(records, "dispatch_summary")
    latencies = [float(r["latency_seconds"]) for r in chunks if isinstance(r.get("latency_seconds"), (int, float))]
    arm_intervals = [float(r["arm_send_interval_p95_seconds"]) for r in dispatch if isinstance(r.get("arm_send_interval_p95_seconds"), (int, float))]
    hand_intervals = [float(r["hand_send_interval_p95_seconds"]) for r in dispatch if isinstance(r.get("hand_send_interval_p95_seconds"), (int, float))]
    if not latencies and not arm_intervals and not hand_intervals:
        return False
    image, draw = _canvas("Inference and dispatch timing", 1280, 560)
    series = (("inference latency", latencies, COLORS["model"]), ("arm interval P95", arm_intervals, COLORS["command"]), ("hand interval P95", hand_intervals, COLORS["measured"]))
    for row, (label, values, color) in enumerate(series):
        top = 90 + row * 145
        box = (230, top, 1220, top + 105)
        draw.rectangle(box, outline=COLORS["grid"])
        draw.text((30, top + 20), label, fill=color, font=_font(14))
        if values:
            array = np.asarray(values, dtype=np.float64)
            draw.text((30, top + 50), f"P50={np.percentile(array, 50):.4f}s  P95={np.percentile(array, 95):.4f}s  max={np.max(array):.4f}s", fill=COLORS["muted"], font=_font(12))
            low, high = float(np.min(array)), float(np.max(array))
            margin = max((high - low) * 0.1, 1e-6)
            segments = _line_segments(
                np.arange(len(array), dtype=np.float64),
                array,
                box,
                (0.0, float(max(1, len(array) - 1))),
                (low - margin, high + margin),
            )
            if segments and len(segments[0]) >= 2:
                draw.line(segments[0], fill=color, width=2)
    image.save(path)
    return True


def _save_boundary(chunks: Sequence[np.ndarray], path: Path) -> bool:
    if len(chunks) < 2:
        return False
    windows: list[np.ndarray] = []
    for previous, current in zip(chunks[:-1], chunks[1:]):
        if len(previous) >= 6 and len(current) >= 12:
            windows.append(np.concatenate((previous[-6:], current[:12]), axis=0))
    if not windows:
        return False
    array = np.stack(windows)
    activity = np.max(np.abs(np.diff(array, axis=1)), axis=2)
    median = np.median(activity, axis=0)
    low = np.percentile(activity, 5, axis=0)
    high = np.percentile(activity, 95, axis=0)
    image, draw = _canvas("Chunk-boundary aligned max joint step", 1280, 430)
    box = (120, 85, 1220, 350)
    draw.rectangle(box, outline=COLORS["grid"])
    maximum = max(float(np.max(high)), 1e-9)
    def point(i: int, value: float) -> tuple[float, float]:
        return (box[0] + i * (box[2] - box[0]) / max(1, len(median) - 1), box[3] - value * (box[3] - box[1]) / maximum)
    polygon = [point(i, float(v)) for i, v in enumerate(low)] + [point(i, float(v)) for i, v in reversed(list(enumerate(high)))]
    draw.polygon(polygon, fill="#d9e3fa")
    draw.line([point(i, float(v)) for i, v in enumerate(median)], fill=COLORS["model"], width=3)
    boundary_x = point(5, 0)[0]
    draw.line((boundary_x, box[1], boundary_x, box[3]), fill=COLORS["danger"], width=2)
    draw.text((boundary_x + 8, box[1] + 5), "chunk boundary", fill=COLORS["danger"], font=_font(13))
    draw.text((120, 370), f"{len(windows)} boundaries; band=P05-P95; model 30Hz", fill=COLORS["muted"], font=_font(13))
    image.save(path)
    return True


def _flatten(chunks: Sequence[np.ndarray]) -> np.ndarray:
    return np.concatenate(chunks, axis=0) if chunks else np.empty((0, 34), dtype=np.float64)


def _feedback_for(source: Path, feedback_root: Path, run_id: str) -> list[dict[str, Any]]:
    candidates = [feedback_root / f"{run_id}.jsonl", feedback_root / f"{source.stem}.jsonl"]
    for candidate in candidates:
        if candidate.exists():
            return _read_jsonl(candidate)[0]
    return []


def _feedback_matrix(records: Sequence[dict[str, Any]]) -> np.ndarray:
    rows: list[np.ndarray] = []
    for record in records:
        row = np.asarray(record.get("joints_rad", []), dtype=np.float64)
        if row.shape == (34,) and np.all(np.isfinite(row)):
            rows.append(row)
    return np.stack(rows) if rows else np.empty((0, 34), dtype=np.float64)


def _feedback_chunks(records: Sequence[dict[str, Any]]) -> list[np.ndarray]:
    grouped: dict[int, list[np.ndarray]] = {}
    for record in records:
        request_id = record.get("request_id")
        row = np.asarray(record.get("joints_rad", []), dtype=np.float64)
        if isinstance(request_id, int) and row.shape == (34,) and np.all(np.isfinite(row)):
            grouped.setdefault(request_id, []).append(row)
    return [np.stack(grouped[key]) for key in sorted(grouped) if grouped[key]]


def _tracking_error_activity(
    dispatch: dict[int, dict[str, Any]],
    feedback: Sequence[dict[str, Any]],
) -> tuple[np.ndarray, float | None]:
    feedback_by_request: dict[int, list[dict[str, Any]]] = {}
    for record in feedback:
        if isinstance(record.get("request_id"), int) and isinstance(record.get("read_completed_monotonic_ns"), int):
            feedback_by_request.setdefault(int(record["request_id"]), []).append(record)
    common = sorted(set(dispatch) & set(feedback_by_request))
    if not common:
        return np.empty((34, 0)), None

    def error_for_lag(lag: float, keep_columns: bool) -> tuple[float, list[np.ndarray]]:
        columns: list[np.ndarray] = []
        all_errors: list[float] = []
        for request_id in common:
            item = dispatch[request_id]
            start = item.get("segment_started_at_monotonic")
            if not isinstance(start, (int, float)):
                continue
            arm = _as_matrix(item.get("arm_commands_rad"), 14)
            hand = _as_matrix(item.get("hand_commands_rad"), 20)
            arm_offsets = np.asarray(item.get("arm_command_time_offsets_seconds", []), dtype=np.float64)
            hand_offsets = np.asarray(item.get("hand_command_time_offsets_seconds", []), dtype=np.float64)
            rows = feedback_by_request[request_id]
            measured = _feedback_matrix(rows)
            measured_times = np.asarray([r["read_completed_monotonic_ns"] / 1e9 for r in rows], dtype=np.float64)
            if not len(measured) or len(measured_times) != len(measured):
                continue
            column = np.full(34, np.nan)
            for begin, values, offsets in ((0, arm, arm_offsets), (14, hand, hand_offsets)):
                if len(values) < 2 or len(offsets) != len(values):
                    continue
                command_times = float(start) + offsets
                query = measured_times - lag
                valid = (query >= command_times[0]) & (query <= command_times[-1])
                if not np.any(valid):
                    continue
                for local in range(values.shape[1]):
                    predicted = np.interp(query[valid], command_times, values[:, local])
                    errors = np.abs(measured[valid, begin + local] - predicted)
                    column[begin + local] = float(np.mean(errors))
                    all_errors.extend(errors.tolist())
            if keep_columns:
                columns.append(column)
        score = float(np.median(all_errors)) if all_errors else math.inf
        return score, columns

    candidates = np.arange(-0.20, 0.201, 0.01)
    scored = [(error_for_lag(float(lag), False)[0], float(lag)) for lag in candidates]
    score, best_lag = min(scored, key=lambda pair: pair[0])
    if not math.isfinite(score):
        return np.empty((34, 0)), None
    _, columns = error_for_lag(best_lag, True)
    return (np.stack(columns, axis=1) if columns else np.empty((34, 0))), best_lag


def _aligned_command_for_feedback(
    dispatch: dict[int, dict[str, Any]],
    feedback: Sequence[dict[str, Any]],
    lag: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    valid_rows = [
        record for record in feedback
        if isinstance(record.get("request_id"), int)
        and isinstance(record.get("read_completed_monotonic_ns"), int)
        and np.asarray(record.get("joints_rad", [])).shape == (34,)
    ]
    valid_rows.sort(key=lambda row: int(row["read_completed_monotonic_ns"]))
    times = np.asarray([row["read_completed_monotonic_ns"] / 1e9 for row in valid_rows], dtype=np.float64)
    measured = _feedback_matrix(valid_rows)
    command = np.full_like(measured, np.nan)
    for row_index, row in enumerate(valid_rows):
        item = dispatch.get(int(row["request_id"]))
        if item is None or not isinstance(item.get("segment_started_at_monotonic"), (int, float)):
            continue
        query = times[row_index] - lag
        start = float(item["segment_started_at_monotonic"])
        for begin, dim, values_key, offsets_key in (
            (0, 14, "arm_commands_rad", "arm_command_time_offsets_seconds"),
            (14, 20, "hand_commands_rad", "hand_command_time_offsets_seconds"),
        ):
            values = _as_matrix(item.get(values_key), dim)
            offsets = np.asarray(item.get(offsets_key, []), dtype=np.float64)
            if len(values) < 2 or len(values) != len(offsets):
                continue
            sample_times = start + offsets
            if sample_times[0] <= query <= sample_times[-1]:
                for local in range(dim):
                    command[row_index, begin + local] = np.interp(query, sample_times, values[:, local])
    return times, command, measured


def _welch_pair(command: np.ndarray, measured: np.ndarray, fs: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
    count = len(command)
    window_size = 64 if count >= 128 else 32
    if count < window_size * 2:
        return None
    window = np.hanning(window_size)
    scale = float(np.sum(window**2) * fs)
    pxx: list[np.ndarray] = []
    pyy: list[np.ndarray] = []
    pxy: list[np.ndarray] = []
    for start in range(0, count - window_size + 1, window_size // 2):
        x = command[start : start + window_size]
        y = measured[start : start + window_size]
        if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
            continue
        xf = np.fft.rfft((x - np.mean(x)) * window)
        yf = np.fft.rfft((y - np.mean(y)) * window)
        pxx.append(np.abs(xf) ** 2 / scale)
        pyy.append(np.abs(yf) ** 2 / scale)
        pxy.append(xf * np.conj(yf) / scale)
    if len(pxx) < 2:
        return None
    command_psd = np.mean(pxx, axis=0)
    measured_psd = np.mean(pyy, axis=0)
    cross = np.mean(pxy, axis=0)
    coherence = np.abs(cross) ** 2 / np.maximum(command_psd * measured_psd, 1e-18)
    return np.fft.rfftfreq(window_size, 1.0 / fs), command_psd, measured_psd, np.clip(coherence, 0.0, 1.0)


def _save_spectrum(
    dispatch: dict[int, dict[str, Any]],
    feedback: Sequence[dict[str, Any]],
    ranking: Sequence[dict[str, Any]],
    lag: float | None,
    path: Path,
) -> bool:
    times, command, measured = _aligned_command_for_feedback(dispatch, feedback, lag or 0.0)
    if len(times) < 64:
        return False
    intervals = np.diff(times)
    median_dt = float(np.median(intervals))
    if median_dt <= 0 or float(np.std(intervals) / median_dt) >= 0.10 or float(np.max(intervals)) > 1.5 * median_dt:
        return False
    fs = 1.0 / median_dt
    rows: list[tuple[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]] = []
    for metric in ranking:
        index = int(metric["index"])
        result = _welch_pair(command[:, index], measured[:, index], fs)
        if result is not None:
            rows.append((str(metric["joint"]), result))
        if len(rows) == 4:
            break
    if not rows:
        return False
    image, draw = _canvas("Welch PSD and command-feedback coherence", 1280, 125 + 190 * len(rows))
    for row, (name, (freq, command_psd, measured_psd, coherence)) in enumerate(rows):
        top = 75 + row * 190
        psd_box = (245, top, 900, top + 135)
        coherence_box = (965, top, 1220, top + 135)
        draw.text((25, top + 15), name, fill=COLORS["text"], font=_font(12))
        draw.rectangle(psd_box, outline=COLORS["grid"])
        draw.rectangle(coherence_box, outline=COLORS["grid"])
        log_command = np.log10(np.maximum(command_psd, 1e-16))
        log_measured = np.log10(np.maximum(measured_psd, 1e-16))
        both = np.concatenate((log_command, log_measured))
        low, high = float(np.min(both)), float(np.max(both))
        high = max(high, low + 1e-6)
        def psd_points(values: np.ndarray) -> list[tuple[float, float]]:
            return [(psd_box[0] + i * (psd_box[2] - psd_box[0]) / max(1, len(values) - 1), psd_box[3] - (float(value) - low) * (psd_box[3] - psd_box[1]) / (high - low)) for i, value in enumerate(values)]
        draw.line(psd_points(log_command), fill=COLORS["command"], width=2)
        draw.line(psd_points(log_measured), fill=COLORS["measured"], width=2)
        coherence_points = [(coherence_box[0] + i * (coherence_box[2] - coherence_box[0]) / max(1, len(coherence) - 1), coherence_box[3] - float(value) * (coherence_box[3] - coherence_box[1])) for i, value in enumerate(coherence)]
        draw.line(coherence_points, fill=COLORS["model"], width=2)
        draw.text((psd_box[0], psd_box[3] + 5), f"0 .. {freq[-1]:.2f}Hz (log PSD)", fill=COLORS["muted"], font=_font(11))
        draw.text((coherence_box[0], coherence_box[3] + 5), "coherence 0..1", fill=COLORS["muted"], font=_font(11))
    draw.text((850, 35), f"sample rate={fs:.2f}Hz; delay={lag or 0.0:+.3f}s", fill=COLORS["muted"], font=_font(12))
    image.save(path)
    return True


def _f710_scalar_series(
    samples: Sequence[dict[str, Any]], key: str
) -> tuple[np.ndarray, np.ndarray]:
    times: list[float] = []
    values: list[float] = []
    for record in samples:
        value = record.get(key)
        stamp = record.get("control_monotonic_ns")
        if isinstance(stamp, int) and isinstance(value, (int, float)) and math.isfinite(float(value)):
            times.append(stamp / 1e9)
            values.append(float(value))
    return np.asarray(times), np.asarray(values)


def _f710_norm_series(
    samples: Sequence[dict[str, Any]], key: str, dim: int
) -> tuple[np.ndarray, np.ndarray]:
    times: list[float] = []
    values: list[float] = []
    for record in samples:
        row = _as_vector(record.get(key), dim)
        stamp = record.get("control_monotonic_ns")
        if isinstance(stamp, int) and len(row):
            times.append(stamp / 1e9)
            values.append(float(np.linalg.norm(row)))
    return np.asarray(times), np.asarray(values)


def _f710_max_series(
    records: Sequence[dict[str, Any]], key: str, dim: int
) -> tuple[np.ndarray, np.ndarray]:
    """Per-tick maximum absolute component, used for limit utilization."""
    times: list[float] = []
    values: list[float] = []
    for record in records:
        row = _as_vector(record.get(key), dim)
        stamp = record.get("control_monotonic_ns")
        if len(row) and isinstance(stamp, int):
            times.append(stamp / 1e9)
            values.append(float(np.max(np.abs(row))))
    return np.asarray(times), np.asarray(values)


def _save_series_panels(
    title: str,
    panels: Sequence[tuple[str, str, Sequence[tuple[str, tuple[np.ndarray, np.ndarray], str]]]],
    path: Path,
) -> bool:
    valid_panels = [
        (name, unit, [(label, series, color) for label, series, color in lines if len(series[0]) and len(series[1])])
        for name, unit, lines in panels
    ]
    valid_panels = [panel for panel in valid_panels if panel[2]]
    if not valid_panels:
        return False
    width = 1280
    row_height = 190
    image, draw = _canvas(title, width, 80 + row_height * len(valid_panels))
    for row_index, (name, unit, lines) in enumerate(valid_panels):
        top = 75 + row_index * row_height
        box = (235, top + 8, 1220, top + 142)
        draw.rectangle(box, outline=COLORS["grid"])
        draw.text((30, top + 12), name, fill=COLORS["text"], font=_font(14))
        draw.text((30, top + 40), unit, fill=COLORS["muted"], font=_font(11))
        all_times = np.concatenate([series[0] for _, series, _ in lines])
        all_values = np.concatenate([series[1] for _, series, _ in lines])
        x_low, x_high = float(np.min(all_times)), float(np.max(all_times))
        y_low, y_high = float(np.min(all_values)), float(np.max(all_values))
        y_margin = max((y_high - y_low) * 0.12, 1e-9)
        x_range = (x_low, max(x_high, x_low + 1e-6))
        y_range = (y_low - y_margin, y_high + y_margin)
        for line_index, (label, (times, values), color) in enumerate(lines):
            for points in _line_segments(times, values, box, x_range, y_range):
                if len(points) >= 2:
                    draw.line(points, fill=color, width=2)
            legend_y = top + 67 + line_index * 22
            draw.line((30, legend_y + 7, 52, legend_y + 7), fill=color, width=3)
            draw.text((59, legend_y), label, fill=color, font=_font(11))
        draw.text(
            (box[0], box[3] + 7),
            f"t=0 … {x_high - x_low:.2f}s · range {y_low:.4g} … {y_high:.4g}",
            fill=COLORS["muted"],
            font=_font(10),
        )
    image.save(path)
    return True


def _save_f710_pipeline(records: Sequence[dict[str, Any]], path: Path) -> bool:
    samples = _f710_samples(records)
    panels = (
        (
            "F710输入",
            "摇杆归一化幅值",
            (
                ("平移", _f710_norm_series(samples, "shaped_linear_axes", 3), COLORS["model"]),
                ("旋转", _f710_norm_series(samples, "shaped_angular_axes", 3), COLORS["danger"]),
            ),
        ),
        (
            "Cartesian平移速度",
            "m/s",
            (
                ("期望", _f710_norm_series(samples, "desired_linear_velocity_m_s", 3), "#9b6bd3"),
                ("slew后", _f710_norm_series(samples, "filtered_linear_velocity_m_s", 3), COLORS["command"]),
            ),
        ),
        (
            "Cartesian旋转速度",
            "rad/s",
            (
                ("期望", _f710_norm_series(samples, "desired_angular_velocity_rad_s", 3), "#9b6bd3"),
                ("slew后", _f710_norm_series(samples, "filtered_angular_velocity_rad_s", 3), COLORS["command"]),
            ),
        ),
        (
            "Pink关节速度reference",
            "||dq_ik||₂ rad/s",
            (("Pink", _f710_norm_series(samples, "ik_dq_ref_rad_s", 7), COLORS["model"]),),
        ),
        (
            "OTG关节速度",
            "||dq||₂ rad/s；未启用OTG时本行不显示",
            (
                ("Pink target", _f710_norm_series(samples, "otg_target_dq_rad_s", 7), COLORS["model"]),
                ("Ruckig output", _f710_norm_series(samples, "otg_dq_cmd_rad_s", 7), COLORS["command"]),
            ),
        ),
        (
            "OTG动力学约束利用率",
            "相对配置上限；1表示命中约束",
            (
                ("acceleration max", _f710_max_series(samples, "otg_acceleration_limit_utilization", 7), "#9b6bd3"),
                ("jerk max", _f710_max_series(samples, "otg_jerk_limit_utilization", 7), COLORS["danger"]),
            ),
        ),
        (
            "链路时序",
            "ms",
            (
                ("packet age", _f710_scalar_series(samples, "packet_age_ms"), "#9b6bd3"),
                ("GDK send", _f710_scalar_series(samples, "gdk_send_duration_ms"), COLORS["measured"]),
            ),
        ),
    )
    return _save_series_panels("F710 → Cartesian → Pink 控制流水线", panels, path)


def _save_f710_constraints(records: Sequence[dict[str, Any]], path: Path) -> bool:
    samples = _f710_samples(records)
    margin_times: list[float] = []
    margins: list[float] = []
    sigma_times: list[float] = []
    sigmas: list[float] = []
    for record in samples:
        stamp = record.get("control_monotonic_ns")
        lower = _as_vector(record.get("joint_lower_margin_rad"), 7)
        upper = _as_vector(record.get("joint_upper_margin_rad"), 7)
        singular = np.asarray(record.get("jacobian_singular_values_raw", []), dtype=np.float64)
        if isinstance(stamp, int) and len(lower) and len(upper):
            margin_times.append(stamp / 1e9)
            margins.append(float(min(np.min(lower), np.min(upper))))
        if isinstance(stamp, int) and singular.ndim == 1 and len(singular) and np.all(np.isfinite(singular)):
            sigma_times.append(stamp / 1e9)
            sigmas.append(float(np.min(singular)))
    panels = (
        (
            "最近关节限位余量",
            "rad；越接近0风险越高",
            (("min margin", (np.asarray(margin_times), np.asarray(margins)), COLORS["danger"]),),
        ),
        (
            "Jacobian最小奇异值",
            "raw 6D metric；只作同构型趋势，不混作物理距离",
            (("sigma min", (np.asarray(sigma_times), np.asarray(sigmas)), "#7c5ce7"),),
        ),
        (
            "IK residual",
            "position m / orientation rad",
            (
                ("position", _f710_scalar_series(samples, "ik_position_error_m"), COLORS["model"]),
                ("orientation", _f710_scalar_series(samples, "ik_orientation_error_rad"), COLORS["command"]),
            ),
        ),
        (
            "安全门事件",
            "每tick计数或0/1状态",
            (
                ("workspace saturated", _f710_scalar_series(samples, "workspace_saturated"), COLORS["danger"]),
                ("clip joints", _f710_scalar_series(samples, "clip_count"), "#9b6bd3"),
            ),
        ),
    )
    return _save_series_panels("Pink约束、奇异性与安全门", panels, path)


def _save_f710_joint_layers(
    records: Sequence[dict[str, Any]],
    feedback: Sequence[dict[str, Any]],
    ranking: Sequence[dict[str, Any]],
    path: Path,
) -> bool:
    command_times, command = _f710_command_trace(records)
    ik_times, ik = _f710_ik_arm_trace(records)
    measured_times, measured = _feedback_trace(feedback)
    arm_ranking = [row for row in ranking if int(row["index"]) < 14][:4]
    if not len(command) or not arm_ranking:
        return False
    image, draw = _canvas("F710关节层：Pink reference → command → measured", 1280, 150 + 215 * len(arm_ranking))
    legend = (("Pink IK", COLORS["model"]), ("command", COLORS["command"]), ("measured", COLORS["measured"]))
    for i, (label, color) in enumerate(legend):
        draw.line((820 + i * 135, 42, 850 + i * 135, 42), fill=color, width=3)
        draw.text((856 + i * 135, 33), label, fill=color, font=_font(12))
    for row_index, metric in enumerate(arm_ranking):
        index = int(metric["index"])
        top = 76 + row_index * 215
        box = (225, top + 20, 1225, top + 170)
        draw.rectangle(box, outline=COLORS["grid"])
        draw.text((30, top + 20), str(metric["joint"]), fill=COLORS["text"], font=_font(12))
        draw.text((30, top + 48), f"reversals={metric['reversals']}", fill=COLORS["muted"], font=_font(11))
        layers = (
            (ik_times, ik[:, index] if len(ik) else np.empty(0), COLORS["model"]),
            (command_times, command[:, index], COLORS["command"]),
            (measured_times, measured[:, index] if len(measured) else np.empty(0), COLORS["measured"]),
        )
        finite_times = [times[np.isfinite(times)] for times, values, _ in layers if len(times) and len(values)]
        finite_values = [values[np.isfinite(values)] for times, values, _ in layers if len(times) and len(values) and np.any(np.isfinite(values))]
        if not finite_times or not finite_values:
            continue
        all_times = np.concatenate(finite_times)
        all_values = np.concatenate(finite_values)
        x_low, x_high = float(np.min(all_times)), float(np.max(all_times))
        y_low, y_high = float(np.min(all_values)), float(np.max(all_values))
        margin = max((y_high - y_low) * 0.1, 1e-4)
        for times, values, color in layers:
            for points in _line_segments(
                times,
                values,
                box,
                (x_low, max(x_high, x_low + 1e-6)),
                (y_low - margin, y_high + margin),
            ):
                if len(points) >= 2:
                    draw.line(points, fill=color, width=2)
        draw.text((box[0], box[3] + 5), f"t=0 … {x_high - x_low:.2f}s", fill=COLORS["muted"], font=_font(10))
    image.save(path)
    return True


def _summary_f710(run: RunData) -> tuple[dict[str, Any], list[np.ndarray], np.ndarray, np.ndarray]:
    records = run.records
    samples = _f710_samples(records)
    dispatch_samples = _f710_dispatch_samples(records)
    _, command = _f710_command_trace(records)
    measured = _feedback_matrix(run.feedback_records)
    command_chunks = [command] if len(command) else []
    start = _started_at(records, run.source)
    control_times = [
        int(record["control_monotonic_ns"]) / 1e9
        for record in dispatch_samples
        if isinstance(record.get("control_monotonic_ns"), int)
    ]
    # F710 recovery may append a synthetic summary minutes after motion stops.
    # Duration therefore belongs to the real control sample timebase, not the
    # wall-clock span of administrative finalization records.
    duration = (
        max(control_times) - min(control_times)
        if len(control_times) >= 2
        else 0.0
    )
    feedback_times = [
        int(record["read_completed_monotonic_ns"]) / 1e9
        for record in run.feedback_records
        if isinstance(record.get("read_completed_monotonic_ns"), int)
        and len(_as_vector(record.get("joints_rad"), 34))
    ]
    feedback_duration = (
        max(feedback_times) - min(feedback_times)
        if len(feedback_times) >= 2
        else 0.0
    )
    feedback_coverage_ratio = (
        min(1.0, feedback_duration / duration) if duration > 0.0 else 0.0
    )
    feedback_disabled = any(
        record.get("status") == "disabled" for record in run.feedback_records
    )
    starts = _events(records, "runtime_start")
    control_hz = _first_number(starts, "control_hz") or ARM_HZ
    input_present = any(
        len(_as_vector(record.get("shaped_linear_axes"), 3)) for record in samples
    )
    cartesian_present = any(
        len(_as_vector(record.get("filtered_linear_velocity_m_s"), 3))
        for record in samples
    )
    ik_present = any(
        len(_as_vector(record.get("ik_q_ref_rad"), 7)) for record in samples
    )
    drop_count = max(
        [int(record.get("telemetry_drop_count", 0) or 0) for record in dispatch_samples]
        + [0]
    )
    workspace_saturated = sum(bool(record.get("workspace_saturated")) for record in dispatch_samples)
    clip_events = sum(int(record.get("clip_count", 0) or 0) for record in dispatch_samples)
    reversal_count = max(
        [int(record.get("reversal_count_total", 0) or 0) for record in dispatch_samples]
        + [0]
    )
    enabled_samples = sum(bool(record.get("enabled")) for record in dispatch_samples)
    otg_enabled = bool(_events(records, "runtime_start")[0].get("otg_enabled")) if _events(records, "runtime_start") else any(bool(record.get("otg_enabled")) for record in samples)
    send_intervals_ms = [
        float(record["send_interval_seconds"]) * 1000.0
        for record in dispatch_samples
        if isinstance(record.get("send_interval_seconds"), (int, float))
    ]
    diagnostic_profile = str(
        (_events(records, "runtime_start") or [{}])[0].get("diagnostic_profile", "full")
    )
    ranking = _metrics(command_chunks, control_hz)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run.run_id,
        "source_relative": run.source_relative,
        "source_hash": run.source_hash,
        "started_at": start,
        "started_iso": datetime.fromtimestamp(start).astimezone().isoformat(timespec="seconds"),
        "date": datetime.fromtimestamp(start).astimezone().strftime("%Y-%m-%d"),
        "mode": _mode(records, run.source),
        "mode_family": "f710",
        "duration_seconds": duration,
        "chunk_count": 0,
        "sample_count": len(dispatch_samples),
        "detail_sample_count": len(samples),
        "enabled_sample_count": enabled_samples,
        "profile": _profile(records, run.source),
        "endpoint": _endpoint(records),
        "model_hz": None,
        "control_hz": control_hz,
        "arm_hz": _first_number(starts, "arm_hz") or ARM_HZ,
        "hand_hz": _first_number(starts, "hand_stream_hz"),
        "layers": {
            "input": input_present,
            "cartesian": cartesian_present,
            "ik": ik_present,
            "command": bool(len(command)),
            "feedback": bool(len(measured)),
        },
        "malformed_lines": run.malformed_lines,
        "feedback_samples": int(len(measured)),
        "feedback_coverage_ratio": feedback_coverage_ratio,
        "feedback_disabled": feedback_disabled,
        "telemetry_drop_count": drop_count,
        "workspace_saturated_samples": workspace_saturated,
        "clip_joint_events": clip_events,
        "reversal_count_total": reversal_count,
        "otg_enabled": otg_enabled,
        "diagnostic_profile": diagnostic_profile,
        "dispatch_interval_p95_ms": (
            None if not send_intervals_ms else float(np.percentile(send_intervals_ms, 95))
        ),
        "dispatch_interval_p99_ms": (
            None if not send_intervals_ms else float(np.percentile(send_intervals_ms, 99))
        ),
        "dispatch_intervals_over_15_ms": sum(
            value > 15.0 for value in send_intervals_ms
        ),
        "recovered": any(
            bool(record.get("recovered"))
            for record in _events(records, "runtime_summary")
        ),
        "ranking_origin": (
            "virtual_100hz_command"
            if "shadow" in _mode(records, run.source).lower()
            else "actual_100hz_command"
        ),
        "ranking": ranking,
    }
    return summary, [], command, measured


def _summary(run: RunData) -> tuple[dict[str, Any], list[np.ndarray], np.ndarray, np.ndarray]:
    records = run.records
    if _mode_family(_mode(records, run.source)) == "f710":
        return _summary_f710(run)
    model_chunks = _model_chunks(records)
    dispatch = _dispatch_by_request(records)
    command_chunks = [_command_chunk(dispatch[key]) for key in sorted(dispatch) if len(_command_chunk(dispatch[key]))]
    model = _flatten(model_chunks)
    command = _flatten(command_chunks)
    measured = _feedback_matrix(run.feedback_records)
    start = _started_at(records, run.source)
    wall_values = [float(r["wall_time"]) for r in records if isinstance(r.get("wall_time"), (int, float))]
    duration = max(wall_values) - min(wall_values) if len(wall_values) >= 2 else 0.0
    mode = _mode(records, run.source)
    hand_hz = _first_number(_events(records, "runtime_start"), "hand_stream_hz")
    ranking = _metrics(model_chunks, MODEL_HZ)
    action_records = [
        row
        for row in _events(records, "action_chunk")
        if len(_as_matrix(row.get("actions"), 34))
    ]
    commitment_exact = bool(action_records) and all(
        isinstance(row.get("used_start_index"), int)
        and isinstance(row.get("used_intervals"), int)
        for row in action_records
    )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run.run_id,
        "source_relative": run.source_relative,
        "source_hash": run.source_hash,
        "started_at": start,
        "started_iso": datetime.fromtimestamp(start).astimezone().isoformat(timespec="seconds"),
        "date": datetime.fromtimestamp(start).astimezone().strftime("%Y-%m-%d"),
        "mode": mode,
        "mode_family": _mode_family(mode),
        "duration_seconds": duration,
        "chunk_count": len(model_chunks),
        "profile": _profile(records, run.source),
        "endpoint": _endpoint(records),
        "model_hz": MODEL_HZ,
        "arm_hz": ARM_HZ,
        "hand_hz": hand_hz,
        "layers": {"model": bool(len(model)), "command": bool(len(command)), "feedback": bool(len(measured))},
        "malformed_lines": run.malformed_lines,
        "feedback_samples": int(len(measured)),
        "model_commitment_origin": "exact" if commitment_exact else "legacy_full_chunk_unknown",
        "command_time_origin": (
            "exact"
            if any(
                isinstance(row.get("segment_started_at_monotonic"), (int, float))
                for row in _events(records, "dispatch_summary")
            )
            else "response_inferred"
        ),
        "ranking": ranking,
    }
    return summary, model_chunks, command, measured


def _live_reload_script(manifest_path: str) -> str:
    return f"""<script>let manifestStamp=null;setInterval(async()=>{{try{{const r=await fetch('{manifest_path}?t='+Date.now(),{{cache:'no-store'}});const m=await r.json();if(manifestStamp===null)manifestStamp=m.generated_at;else if(manifestStamp!==m.generated_at)location.reload();}}catch(_e){{}}}},2000);</script>"""


def _f710_report_html(summary: dict[str, Any], figures: Sequence[str]) -> str:
    badges = "".join(
        f'<span class="badge {"yes" if present else "no"}">{html.escape(name.upper())}</span>'
        for name, present in summary["layers"].items()
    )
    status_cards = (
        ("Dispatch样本", str(summary.get("sample_count", 0)), "真实100 Hz时基"),
        ("分层样本", str(summary.get("detail_sample_count", 0)), summary.get("diagnostic_profile", "full")),
        (
            "Dispatch P95",
            "-" if summary.get("dispatch_interval_p95_ms") is None else f"{summary['dispatch_interval_p95_ms']:.2f} ms",
            f">15ms: {summary.get('dispatch_intervals_over_15_ms', 0)}",
        ),
        ("工作区饱和", str(summary.get("workspace_saturated_samples", 0)), "samples"),
        ("物理限位裁剪", str(summary.get("clip_joint_events", 0)), "joint events"),
        ("小反转累计", str(summary.get("reversal_count_total", 0)), "command前统计"),
        ("反馈样本", str(summary.get("feedback_samples", 0)), "probe-gated"),
        ("日志丢样", str(summary.get("telemetry_drop_count", 0)), "必须为0"),
    )
    cards = "".join(
        f'<article class="metric"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong><small>{html.escape(note)}</small></article>'
        for label, value, note in status_cards
    )
    coverage = "".join(
        f'<tr><td>{html.escape(str(row["key"]))}</td><td>{html.escape(str(row["name"]))}</td><td><span class="coverage {"ok" if row["status"] == "完整" else "partial"}">{html.escape(str(row["status"]))}</span></td><td>{html.escape(str(row["detail"]))}</td></tr>'
        for row in summary.get("diagnostic_coverage", [])
    )
    figure_labels = {
        "f710_pipeline.png": "T1–T3 · 操作输入、Cartesian与Pink流水线",
        "f710_joint_layers.png": "T3–T5 · Pink reference、实际command与真机反馈",
        "f710_constraints.png": "T3–T4 · 限位、奇异趋势与安全门",
    }
    images = "".join(
        f'<section id="{name.removesuffix(".png")}"><div class="section-head"><h2>{html.escape(figure_labels.get(name, name))}</h2><a href="#top">回到顶部</a></div><img src="figures/{html.escape(name)}" loading="lazy"></section>'
        for name in figures
    )
    rows = "".join(
        "<tr>" + "".join(
            f"<td>{html.escape(str(value))}</td>" for value in (
                row["joint"], f"{row['net_rad']:.5f}", f"{row['travel_rad']:.5f}",
                f"{row['travel_ratio']:.1f}", row["reversals"], f"{row['step_p95_rad']:.5f}",
                "-" if row["max_jerk_rad_s3"] is None else f"{row['max_jerk_rad_s3']:.2f}",
            )
        ) + "</tr>" for row in summary["ranking"] if int(row["index"]) < 14
    )
    current_payload = json.dumps(
        {"run_id": summary["run_id"], "mode": summary["mode"], "mode_family": "f710"},
        ensure_ascii=False,
    ).replace("</", "<\\/")
    otg = "已接入" if summary.get("otg_enabled") else "尚未接入（当前Pink reference直接进入command）"
    shadow = "shadow" in str(summary.get("mode", "")).lower()
    run_kind = "ZERO-OUTPUT F710 OTG SHADOW" if shadow else "POWERED F710 TELEOP"
    ranking_title = "虚拟双臂command活动排名" if shadow else "实际双臂command活动排名"
    ranking_note = "ZERO hardware output；不是VLA model输出" if shadow else "不是VLA model输出"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(summary['run_id'])}</title><link rel="stylesheet" href="../../assets/style.css"></head><body class="family-f710"><main id="top"><nav class="detail-nav"><a class="button" href="../../index.html">← 全部运行</a><span class="mode-pill f710">F710 摇操</span><a href="#f710_pipeline">控制流水线</a><a href="#f710_joint_layers">关节分层</a><a href="#f710_constraints">约束</a><button id="chooseCompare">选择同类运行对比</button></nav><header class="hero f710"><p class="eyebrow">{run_kind}</p><h1>{html.escape(summary['started_iso'])}</h1><p>{badges}</p><div class="meta"><span>{summary.get('sample_count', 0)} control samples</span><span>{summary['duration_seconds']:.1f}s</span><span>{html.escape(summary['profile'])}</span><span>{html.escape(summary['endpoint'])}</span></div></header><div class="metrics">{cards}</div><aside class="reading-guide"><h2>从哪一层开始抖？</h2><div class="pipeline"><span>F710 input</span><b>→</b><span>Cartesian</span><b>→</b><span>Pink IK</span><b>→</b><span>command</span><b>→</b><span>feedback</span></div><ol><li>先看控制流水线：输入稳定而Pink dq反转，优先查IK、限位和奇异区。</li><li>再看关节分层：Pink稳定而command新增反转，查输出整形；command稳定而feedback振荡，查Servo或机构。</li><li>最后看约束图：确认反转是否和workspace、clip、joint margin或奇异趋势同步。</li></ol><p><strong>OTG：</strong>{html.escape(otg)}。缺失层明确显示，不用滤波把问题藏起来。</p></aside><section><div class="section-head"><h2>本轮数据覆盖</h2><span class="foot">F710专属T1–T6，不占用VLA的A–H定义</span></div><div class="table"><table><thead><tr><th>层</th><th>诊断</th><th>状态</th><th>说明</th></tr></thead><tbody>{coverage}</tbody></table></div></section>{images}<section><div class="section-head"><h2>{ranking_title}</h2><span class="foot">{ranking_note}</span></div><div class="table"><table><thead><tr><th>Joint</th><th>Net(rad)</th><th>Travel(rad)</th><th>Travel/net</th><th>Reversals</th><th>Step P95</th><th>Max jerk</th></tr></thead><tbody>{rows}</tbody></table></div></section><p class="foot">source: {html.escape(summary['source_relative'])} · malformed lines: {summary['malformed_lines']}</p></main><dialog id="compareDialog"><div class="dialog-head"><div><h2>选择另一轮F710运行</h2><p>默认只显示相同控制家族，避免把不同语义的曲线误作配对实验。</p></div><button id="closeCompare">关闭</button></div><input id="compareSearch" class="wide" placeholder="搜索日期或profile"><div id="candidateRuns" class="candidate-runs"></div></dialog><script>const CURRENT={current_payload};const dialog=document.querySelector('#compareDialog'),candidateRuns=document.querySelector('#candidateRuns'),compareSearch=document.querySelector('#compareSearch');let allRuns=[];function card(r){{return `<article class="candidate"><div><strong>${{r.started_iso}}</strong><br><small>${{r.profile}} · ${{r.duration_seconds.toFixed(1)}}s</small></div><button onclick="location.href='../../compare.html?base=${{encodeURIComponent(CURRENT.run_id)}}&candidate=${{encodeURIComponent(r.run_id)}}'">对比</button></article>`}}function render(){{const q=compareSearch.value.toLowerCase();candidateRuns.innerHTML=allRuns.filter(r=>r.run_id!==CURRENT.run_id&&r.mode_family==='f710'&&(!q||JSON.stringify(r).toLowerCase().includes(q))).map(card).join('')||'<p>还没有另一轮F710结构化日志。</p>'}}async function openCompare(){{if(!allRuns.length)allRuns=(await (await fetch('../../manifest.json?t='+Date.now(),{{cache:'no-store'}})).json()).runs;render();dialog.showModal();}}document.querySelector('#chooseCompare').onclick=openCompare;document.querySelector('#closeCompare').onclick=()=>dialog.close();compareSearch.oninput=render;</script>{_live_reload_script('../../manifest.json')}</body></html>"""


def _report_html(summary: dict[str, Any], figures: Sequence[str]) -> str:
    if summary.get("mode_family") == "f710":
        return _f710_report_html(summary, figures)
    badges = "".join(
        f'<span class="badge {"yes" if present else "no"}">{name.upper()}</span>'
        for name, present in summary["layers"].items()
    )
    rows = "".join(
        "<tr>" + "".join(
            f"<td>{html.escape(str(value))}</td>" for value in (
                row["joint"], f"{row['net_rad']:.5f}", f"{row['travel_rad']:.5f}",
                f"{row['travel_ratio']:.1f}", row["reversals"], f"{row['step_p95_rad']:.5f}",
                "-" if row["max_jerk_rad_s3"] is None else f"{row['max_jerk_rad_s3']:.2f}",
            )
        ) + "</tr>" for row in summary["ranking"]
    )
    figure_labels = {
        "activity_heatmap.png": "A · 34DoF活动热图",
        "top_joints.png": "B · 双臂/双手问题关节三层轨迹",
        "tracking_error.png": "C · 延迟对齐后的命令—反馈误差",
        "chunk_boundary.png": "D · chunk边界对齐统计",
        "spectrum_coherence.png": "E · PSD / coherence",
        "timing.png": "H · 推理与发送时序（部分）",
    }
    images = "".join(
        f'<section><h2>{html.escape(figure_labels.get(name, name))}</h2><img src="figures/{html.escape(name)}" loading="lazy"></section>'
        for name in figures
    )
    unavailable = []
    if not summary["layers"]["command"]:
        unavailable.append("旧日志没有完整实际下发数组：命令层图不可计算。")
    if not summary["layers"]["feedback"]:
        unavailable.append("没有34维连续硬件反馈：tracking error、PSD/coherence不可计算。")
    elif not summary.get("spectral_valid"):
        unavailable.append("反馈不满足连续、均匀且足够长的采样门：PSD/coherence不可计算。")
    if summary.get("command_time_origin") == "response_inferred":
        unavailable.append("旧日志缺少segment开始单调时钟；命令时间以模型响应时刻近似，报告已明确降级。")
    if summary.get("model_commitment_origin") != "exact":
        unavailable.append("旧日志缺少used_start_index/used_intervals；无法区分已提交前缀和未执行预测，本轮model图按整段chunk降级展示。")
    note = "".join(f"<li>{html.escape(item)}</li>" for item in unavailable) or "<li>三层数据齐全；仍需结合视频判断编码器不可观测振动。</li>"
    coverage = "".join(
        f'<tr><td>{html.escape(str(row["key"]))}</td><td>{html.escape(str(row["name"]))}</td><td><span class="coverage {"ok" if row["status"] == "完整" else "partial"}">{html.escape(str(row["status"]))}</span></td><td>{html.escape(str(row["detail"]))}</td></tr>'
        for row in summary.get("diagnostic_coverage", [])
    )
    current_payload = json.dumps(
        {"run_id": summary["run_id"], "mode": summary["mode"]}, ensure_ascii=False
    ).replace("</", "<\\/")
    model_scope = (
        "scheduler实际提交的waypoint前缀"
        if summary.get("model_commitment_origin") == "exact"
        else "整段模型chunk（旧日志缺commitment字段，无法确认实际提交范围）"
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(summary['run_id'])}</title><link rel="stylesheet" href="../../assets/style.css"></head><body><main><div class="toolbar"><a class="button" href="../../index.html">← 返回全部运行</a><button id="chooseCompare">选择另一轮对比</button></div><h1>{html.escape(summary['started_iso'])}</h1><p>{badges}</p><div class="meta"><span>{html.escape(summary['mode'])}</span><span>{summary['chunk_count']} chunks</span><span>{summary['duration_seconds']:.1f}s</span><span>{html.escape(summary['profile'])}</span><span>{html.escape(summary['endpoint'])}</span></div><aside><h2>怎么看</h2><ol><li>先看活动热图，判断异常集中于手、臂、chunk内部还是边界。</li><li>再看同一真实时间轴和统一坐标下的轨迹：model先反转偏向模型；command新增反转偏向插值/拼接；command稳定而measured振荡偏向闭环或机构。</li><li>发送时序出现尖峰时，先排除推理等待或控制deadline，不要直接加滤波。</li></ol><p><strong>本轮model口径：</strong>{html.escape(model_scope)}。</p><ul>{note}</ul></aside><section><h2>诊断覆盖 A–H</h2><p>“尚未实现”和“不可计算”会明确显示，不用从缺图猜原因。</p><div class="table"><table><thead><tr><th>项</th><th>诊断</th><th>本轮状态</th><th>说明</th></tr></thead><tbody>{coverage}</tbody></table></div></section>{images}<h2>model 关节活动排名</h2><p class="foot">统计对象：{html.escape(model_scope)}；它不是硬件实测。</p><div class="table"><table><thead><tr><th>Joint</th><th>Net(rad)</th><th>Travel(rad)</th><th>Travel/net</th><th>Reversals</th><th>Step P95</th><th>Max jerk</th></tr></thead><tbody>{rows}</tbody></table></div><p class="foot">source: {html.escape(summary['source_relative'])} · malformed lines: {summary['malformed_lines']}</p></main>
<dialog id="compareDialog"><div class="dialog-head"><div><h2>选择另一轮对比</h2><p>当前详情保持为基线；从候选卡片选择另一轮。</p></div><button id="closeCompare">关闭</button></div><input id="compareSearch" class="wide" placeholder="搜索日期、模式、profile或端口"><div id="recommend"></div><div id="candidateRuns" class="candidate-runs"></div></dialog>
<script>
const CURRENT={current_payload};const dialog=document.querySelector('#compareDialog'),candidateRuns=document.querySelector('#candidateRuns'),recommend=document.querySelector('#recommend'),compareSearch=document.querySelector('#compareSearch');let allRuns=[];
function candidateCard(r,recommended=false){{return `<article class="candidate"><div><strong>${{recommended?'推荐 · ':''}}${{r.started_iso}}</strong><br><small>${{r.mode}} · ${{r.chunk_count}} chunks · ${{r.duration_seconds.toFixed(1)}}s<br>${{r.profile}} · ${{r.endpoint}}</small></div><button onclick="selectCandidate('${{r.run_id}}')">与这一轮比较</button></article>`}}
function renderCandidates(){{const q=compareSearch.value.toLowerCase();candidateRuns.innerHTML=allRuns.filter(r=>r.run_id!==CURRENT.run_id&&(!q||JSON.stringify(r).toLowerCase().includes(q))).map(r=>candidateCard(r)).join('')||'<p>没有匹配运行。</p>'}}
function selectCandidate(id){{location.href=`../../compare.html?base=${{encodeURIComponent(CURRENT.run_id)}}&candidate=${{encodeURIComponent(id)}}`}}
async function openCompare(){{if(!allRuns.length){{const response=await fetch('../../manifest.json?t='+Date.now(),{{cache:'no-store'}});allRuns=(await response.json()).runs;const index=allRuns.findIndex(r=>r.run_id===CURRENT.run_id);const previous=allRuns.slice(index+1).find(r=>r.mode===CURRENT.mode);recommend.innerHTML=previous?`<h3>推荐：上一轮同模式</h3>${{candidateCard(previous,true)}}`:'';renderCandidates();}}dialog.showModal();compareSearch.focus();}}
document.querySelector('#chooseCompare').onclick=openCompare;document.querySelector('#closeCompare').onclick=()=>dialog.close();compareSearch.oninput=renderCandidates;if(new URLSearchParams(location.search).has('compare'))openCompare();
</script>{_live_reload_script('../../manifest.json')}</body></html>"""


def _style() -> str:
    return """:root{--ink:#172033;--muted:#657087;--line:#d8dee9;--panel:#fff;--bg:#f3f6fa;--sync:#3468d4;--rtc:#7c5ce7;--f710:#df7a32}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);font:15px system-ui,-apple-system,"Segoe UI",sans-serif}main{max-width:1380px;margin:auto;padding:28px}h1{letter-spacing:-.025em}h2{letter-spacing:-.015em}a{color:#3468d4}.button{display:inline-block;text-decoration:none;color:var(--ink);padding:9px 12px;border:1px solid #c9d0dc;border-radius:9px;background:#fff}.toolbar,.meta,.detail-nav{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:14px 0}.detail-nav{position:sticky;top:0;z-index:10;margin:0 -12px 18px;padding:10px 12px;background:#f3f6faeF;backdrop-filter:blur(12px);border-bottom:1px solid #d8dee9}.detail-nav a:not(.button){text-decoration:none}.detail-nav button{margin-left:auto}.meta span,.badge,.mode-pill{background:#fff;border:1px solid var(--line);border-radius:999px;padding:6px 10px}.badge{font-size:12px;font-weight:700;letter-spacing:.04em}.badge.yes{background:#e8f7f1;color:#087554}.badge.no{background:#f1f3f6;color:#7d8799}.mode-pill{font-weight:750}.mode-pill.sync{color:var(--sync);background:#edf3ff}.mode-pill.rtc{color:var(--rtc);background:#f3efff}.mode-pill.f710{color:#a94d0d;background:#fff1e6}.coverage{display:inline-block;padding:4px 8px;border-radius:7px;background:#fff1d7;color:#8a5a00}.coverage.ok{background:#e8f7f1;color:#087554}aside,section,.day,.hero{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;margin:18px 0;box-shadow:0 4px 18px #18243b0b}.hero{padding:24px;border-left:6px solid #9aa4b5}.hero.f710{border-left-color:var(--f710);background:linear-gradient(115deg,#fff 55%,#fff3e9)}.eyebrow{font-weight:800;font-size:12px;letter-spacing:.14em;color:var(--f710)}.reading-guide{border-left:5px solid var(--f710)}.pipeline{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:14px 0}.pipeline span{padding:8px 10px;border-radius:9px;background:#f3f6fa;border:1px solid var(--line)}.metrics{display:grid;grid-template-columns:repeat(6,minmax(130px,1fr));gap:10px}.metric{display:flex;flex-direction:column;gap:4px;padding:14px;background:#fff;border:1px solid var(--line);border-radius:12px}.metric span,.metric small{color:var(--muted)}.metric strong{font-size:24px}.section-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.section-head h2{margin:0}.family-tabs{display:grid;grid-template-columns:repeat(3,minmax(180px,1fr));gap:12px;margin:18px 0}.family-tab{display:flex;align-items:center;justify-content:space-between;padding:15px;border-radius:12px;border:1px solid var(--line);background:#fff;cursor:pointer}.family-tab strong{font-size:16px}.family-tab.sync{border-left:6px solid var(--sync)}.family-tab.rtc{border-left:6px solid var(--rtc)}.family-tab.f710{border-left:6px solid var(--f710)}.family-tab.active{box-shadow:0 0 0 3px #17203312}.family-tab small{color:var(--muted)}img{display:block;max-width:100%;height:auto;margin:auto}.runs{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}.run{position:relative;border:1px solid var(--line);border-top:5px solid #9aa4b5;border-radius:12px;padding:15px;background:#fff;transition:transform .15s ease,box-shadow .15s ease}.run:hover{transform:translateY(-2px);box-shadow:0 9px 25px #17203316}.run.sync{border-top-color:var(--sync)}.run.rtc{border-top-color:var(--rtc)}.run.f710{border-top-color:var(--f710)}.run h3{margin:0 0 8px}.run small,.candidate small{color:var(--muted)}.run-stats{display:flex;gap:12px;color:var(--muted);margin:9px 0}.compare-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.compare-grid iframe{width:100%;height:78vh;border:1px solid var(--line);border-radius:8px;background:#fff}.table{overflow:auto}table{border-collapse:collapse;width:100%;background:#fff}th,td{border-bottom:1px solid #e3e7ee;padding:9px;text-align:left;white-space:nowrap}th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}.foot{color:var(--muted);word-break:break-all}input,select,button{font:inherit;padding:9px 11px;border:1px solid #c9d0dc;border-radius:9px;background:#fff;cursor:pointer}.wide{width:100%;margin:8px 0 14px}.empty{padding:32px;text-align:center;color:var(--muted);background:#fff;border:1px dashed #c9d0dc;border-radius:12px}dialog{width:min(920px,calc(100vw - 40px));max-height:82vh;border:1px solid #c9d0dc;border-radius:14px;padding:18px;box-shadow:0 20px 70px #17203355}dialog::backdrop{background:#17203388}.dialog-head,.candidate{display:flex;align-items:center;justify-content:space-between;gap:16px}.dialog-head h2{margin:0}.candidate-runs{display:grid;gap:8px;margin-top:12px}.candidate{border:1px solid var(--line);border-radius:9px;padding:11px}.candidate button{white-space:nowrap}@media(max-width:1000px){.metrics{grid-template-columns:repeat(3,1fr)}}@media(max-width:700px){main{padding:14px}.runs,.compare-grid,.family-tabs{grid-template-columns:1fr}.compare-grid iframe{height:65vh}.candidate{align-items:flex-start;flex-direction:column}.metrics{grid-template-columns:repeat(2,1fr)}.detail-nav a:not(.button){display:none}.detail-nav button{margin-left:0}.section-head{align-items:flex-start;flex-direction:column}}"""


def _index_html(runs: Sequence[dict[str, Any]]) -> str:
    payload = json.dumps(runs, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>G2运动诊断</title><link rel="stylesheet" href="assets/style.css"></head><body><main>
<h1>G2运动诊断平台</h1>
<p>Sync、RTC和F710摇操按控制家族分色；徽标只表示本轮真正保存的数据层，缺失字段不会补造。</p>
<div class="family-tabs">
  <button class="family-tab sync" data-family="sync"><strong>Sync</strong><small id="syncCount"></small></button>
  <button class="family-tab rtc" data-family="rtc"><strong>RTC</strong><small id="rtcCount"></small></button>
  <button class="family-tab f710" data-family="f710"><strong>F710 摇操</strong><small id="f710Count"></small></button>
</div>
<div class="toolbar">
  <button id="allFamilies">全部运行</button><button id="latest">打开最新</button>
  <select id="mode"><option value="">全部具体模式</option><option>powered-sync</option><option>powered-rtc</option><option>powered-f710</option><option>powered-async</option><option>rtc-shadow</option><option>shadow</option></select>
  <select id="layer"><option value="">全部数据层</option><option value="model">MODEL</option><option value="input">INPUT</option><option value="cartesian">CARTESIAN</option><option value="ik">IK</option><option value="command">COMMAND</option><option value="feedback">FEEDBACK</option></select>
  <input id="search" placeholder="搜索日期、profile、端口或run">
</div><p class="foot">要做对比：先进入任意一轮详情，再点“选择另一轮对比”。</p><div id="content"></div></main>
<script>
const RUNS={payload};
const content=document.querySelector('#content'),mode=document.querySelector('#mode'),layer=document.querySelector('#layer'),search=document.querySelector('#search');let family='';
function reportUrl(r){{return `runs/${{r.run_id}}/report.html`}}
function badges(r){{return Object.entries(r.layers).map(([k,v])=>`<span class="badge ${{v?'yes':'no'}}">${{k.toUpperCase()}}</span>`).join(' ')}}
function familyName(f){{return f==='f710'?'F710 摇操':f==='rtc'?'RTC':f==='sync'?'Sync':'其他'}}
function card(r){{const amount=r.mode_family==='f710'?`${{r.sample_count||0}} samples`:`${{r.chunk_count}} chunks`;return `<article class="run ${{r.mode_family||'other'}}"><span class="mode-pill ${{r.mode_family||'other'}}">${{familyName(r.mode_family)}}</span><h3><a href="${{reportUrl(r)}}">${{r.started_iso}}</a></h3><p>${{badges(r)}}</p><div class="run-stats"><span>${{r.mode}}</span><span>${{amount}}</span><span>${{r.duration_seconds.toFixed(1)}}s</span></div><small>${{r.profile}}<br>${{r.endpoint}}</small><p><a class="button" href="${{reportUrl(r)}}">打开本轮分析 →</a></p></article>`}}
function setFamily(next){{family=next;document.querySelectorAll('.family-tab').forEach(b=>b.classList.toggle('active',b.dataset.family===family));render()}}
function render(){{const q=search.value.toLowerCase();const selected=RUNS.filter(r=>(!family||r.mode_family===family)&&(!mode.value||r.mode===mode.value)&&(!layer.value||r.layers[layer.value])&&(!q||JSON.stringify(r).toLowerCase().includes(q)));const days={{}};for(const r of selected)(days[r.date]??=[]).push(r);content.innerHTML=Object.entries(days).map(([d,rs])=>`<section class="day"><div class="section-head"><h2>${{d}}</h2><span class="foot">${{rs.length}} runs</span></div><div class="runs">${{rs.map(card).join('')}}</div></section>`).join('')||'<div class="empty">当前筛选下没有运行。F710结构化卡片会在下一次日志拉取后自动出现。</div>'}}
for(const f of ['sync','rtc','f710'])document.querySelector('#'+f+'Count').textContent=RUNS.filter(r=>r.mode_family===f).length+' runs';document.querySelectorAll('.family-tab').forEach(b=>b.onclick=()=>setFamily(b.dataset.family));document.querySelector('#allFamilies').onclick=()=>setFamily('');
document.querySelector('#latest').onclick=()=>{{if(RUNS[0])location.href=reportUrl(RUNS[0])}};
mode.onchange=layer.onchange=search.oninput=render;render();
</script>{_live_reload_script('manifest.json')}</body></html>"""


def _compare_html() -> str:
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>运行对比</title><link rel="stylesheet" href="assets/style.css"></head><body><main><div class="toolbar"><a class="button" href="index.html">← 全部运行</a><a class="button" id="backBase" href="index.html">返回当前基线</a><a class="button" id="changeCandidate" href="index.html">更换对比运行</a></div><h1>运行并排对比</h1><p id="labels"></p><aside><strong>科学口径：</strong>不同真机rollout只能做描述性比较。只有同轨迹、同初始条件的配对实验，才能生成改善/退化比例并讨论因果。</aside><div id="comparison" class="compare-grid"></div></main><script>
const params=new URLSearchParams(location.search),base=params.get('base'),candidate=params.get('candidate');
async function init(){const manifest=await (await fetch('manifest.json?t='+Date.now(),{cache:'no-store'})).json();const a=manifest.runs.find(r=>r.run_id===base),b=manifest.runs.find(r=>r.run_id===candidate);if(!a||!b||a.run_id===b.run_id){document.querySelector('#comparison').innerHTML='<p>对比参数无效，请返回详情页重新选择。</p>';return;}document.querySelector('#labels').textContent=`当前基线：${a.started_iso}  ↔  对比运行：${b.started_iso}`;document.querySelector('#backBase').href=`runs/${a.run_id}/report.html`;document.querySelector('#changeCandidate').href=`runs/${a.run_id}/report.html?compare=1`;document.querySelector('#comparison').innerHTML=`<iframe title="当前基线" src="runs/${a.run_id}/report.html"></iframe><iframe title="对比运行" src="runs/${b.run_id}/report.html"></iframe>`;}init();
</script></body></html>"""


def build_one(source: Path, log_root: Path, output_root: Path) -> dict[str, Any]:
    digest = _sha256(source)
    relative = source.relative_to(log_root).as_posix()
    run_id = _run_id(relative, digest)
    records, malformed = _read_jsonl(source)
    feedback = _feedback_for(source, log_root / "telemetry" / "feedback", run_id)
    run = RunData(source, relative, digest, run_id, records, malformed, feedback)
    summary, model_chunks, command, measured = _summary(run)
    destination = output_root / "runs" / run_id
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=destination.parent))
    try:
        figures_dir = temporary / "figures"
        figures_dir.mkdir()
        dispatch = _dispatch_by_request(records)
        command_chunks = [_command_chunk(dispatch[key]) for key in sorted(dispatch) if len(_command_chunk(dispatch[key]))]
        measured_chunks = _feedback_chunks(feedback)
        figure_names: list[str] = []
        ranking = summary["ranking"]
        if summary["mode_family"] == "f710":
            if _save_f710_pipeline(records, figures_dir / "f710_pipeline.png"):
                figure_names.append("f710_pipeline.png")
            if _save_f710_constraints(records, figures_dir / "f710_constraints.png"):
                figure_names.append("f710_constraints.png")
            if _save_f710_joint_layers(
                records,
                feedback,
                ranking,
                figures_dir / "f710_joint_layers.png",
            ):
                figure_names.append("f710_joint_layers.png")
            summary["spectral_valid"] = False
            feedback_status = (
                "完整"
                if summary["layers"]["feedback"]
                and not summary.get("feedback_disabled")
                and float(summary.get("feedback_coverage_ratio", 0.0)) >= 0.9
                else (
                    "部分"
                    if summary["layers"]["feedback"]
                    else (
                        "实验性关闭"
                        if summary.get("diagnostic_profile") == "timing-ab"
                        else "不可计算"
                    )
                )
            )
            summary["diagnostic_coverage"] = [
                {"key": "T1", "name": "F710输入", "status": "完整" if summary["layers"]["input"] else "不可计算", "detail": "显示Windows端映射后、实际送入G2的平移/旋转意图；不是原始USB轴值。"},
                {"key": "T2", "name": "Cartesian目标", "status": "完整" if summary["layers"]["cartesian"] else "不可计算", "detail": "期望速度、150ms slew后速度和目标lead使用同一G2单调时间。"},
                {"key": "T3", "name": "Pink reference", "status": "完整" if summary["layers"]["ik"] else "不可计算", "detail": "保存q/dq reference、residual、关节margin与raw Jacobian奇异值趋势。"},
                {"key": "T4", "name": ("虚拟command" if "shadow" in summary["mode"].lower() else "实际command"), "status": "完整" if summary["layers"]["command"] else "不可计算", "detail": ("Shadow中的100Hz虚拟OTG输出；明确没有进入GDK。" if "shadow" in summary["mode"].lower() else "timing-ab也保留每次真实100Hz双臂command和GDK调用起止时刻；20Hz分层降采样不会伪装成100Hz。")},
                {"key": "T5", "name": "硬件feedback", "status": feedback_status, "detail": ("timing-ab为隔离反馈读取对deadline的影响而显式关闭T5；这不是反馈缺失故障。" if summary.get("diagnostic_profile") == "timing-ab" else f"有效反馈覆盖约{100.0 * float(summary.get('feedback_coverage_ratio', 0.0)):.1f}%；读取门自动关闭={bool(summary.get('feedback_disabled'))}。")},
                {"key": "T6", "name": "OTG状态", "status": "完整" if summary["otg_enabled"] else "尚未接入", "detail": ("显示Pink目标速度、Ruckig输出q/dq/ddq/jerk；timing-ab以20Hz记录该分层，100Hz command仍完整。" if summary["otg_enabled"] else "当前仍是Pink reference直接下发；页面不会把缺失的OTG状态伪装成已实现。")},
            ]
        else:
            if _save_heatmap((("model", _activity(model_chunks)), ("command", _activity(command_chunks)), ("measured", _activity(measured_chunks))), figures_dir / "activity_heatmap.png", title="34DoF activity heatmap (committed model / actual command / measured)"):
                figure_names.append("activity_heatmap.png")
            if _save_top_joints(
                _model_trace(records),
                _command_traces(records),
                _feedback_trace(feedback),
                ranking,
                figures_dir / "top_joints.png",
            ):
                figure_names.append("top_joints.png")
            if _save_boundary(model_chunks, figures_dir / "chunk_boundary.png"):
                figure_names.append("chunk_boundary.png")
            if _save_timing(records, figures_dir / "timing.png"):
                figure_names.append("timing.png")
            tracking, lag = _tracking_error_activity(dispatch, feedback)
            summary["tracking_delay_seconds"] = lag
            if _save_heatmap((("tracking error", tracking),), figures_dir / "tracking_error.png", title="Delay-aligned mean |q_measured - q_command| per request"):
                figure_names.append("tracking_error.png")
            spectral_valid = _save_spectrum(
                dispatch, feedback, ranking, lag, figures_dir / "spectrum_coherence.png"
            )
            summary["spectral_valid"] = spectral_valid
            if spectral_valid:
                figure_names.append("spectrum_coherence.png")
            summary["diagnostic_coverage"] = [
                {"key": "A", "name": "34DoF活动热图", "status": ("完整" if summary.get("model_commitment_origin") == "exact" else "部分") if summary["layers"]["model"] else "不可计算", "detail": "有commitment字段时只统计实际提交的model前缀；命令/反馈按日志完整性追加。"},
                {"key": "B", "name": "三层问题关节轨迹", "status": "完整" if all(summary["layers"].values()) else "部分", "detail": "真实单调时间、同关节统一坐标；臂和手各选Top-2。"},
                {"key": "C", "name": "命令—反馈误差", "status": "完整" if "tracking_error.png" in figure_names else "不可计算", "detail": "需要连续硬件反馈并做延迟对齐。"},
                {"key": "D", "name": "chunk边界统计", "status": ("完整" if summary.get("model_commitment_origin") == "exact" else "部分") if "chunk_boundary.png" in figure_names else "不可计算", "detail": "新日志使用scheduler实际提交的model waypoint；旧日志缺字段时明确降级。"},
                {"key": "E", "name": "PSD / coherence", "status": "完整" if spectral_valid else "不可计算", "detail": "只在反馈采样均匀、连续且Nyquist足够时生成。"},
                {"key": "F", "name": "RTC plan disagreement", "status": "尚未实现", "detail": "需要日志同时保存新旧重叠plan；不能由普通Sync日志推断。"},
                {"key": "G", "name": "配对A/B统计", "status": "尚未实现", "detail": "不同真机rollout当前只能描述性并排，不能当作同轨迹因果A/B。"},
                {"key": "H", "name": "时序瀑布 / deadline", "status": "部分", "detail": "已有推理、arm/hand间隔；尚缺逐请求完整waterfall与deadline raster。"},
            ]
        summary["figures"] = figure_names
        (temporary / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        (temporary / "report.html").write_text(_report_html(summary, figure_names), encoding="utf-8")
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return summary


def discover(log_root: Path) -> list[Path]:
    paths: list[Path] = []
    for category in ("powered", "shadow"):
        root = log_root / "telemetry" / category
        if root.exists():
            paths.extend(
                path for path in root.rglob("*.jsonl") if _is_reportable_source(path)
            )
    return sorted(set(paths))


def _is_reportable_source(path: Path) -> bool:
    """Exclude F710 startup faults that contain no actual control sample.

    The raw fault JSONL remains untouched for incident diagnosis.  It simply
    must not masquerade as a zero-second motion run in the research dashboard.
    Other telemetry families retain their legacy discovery behavior.
    """
    if "f710" not in path.name.lower():
        return True
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(record, dict)
                    and record.get("event") == "f710_control_sample"
                ):
                    return True
    except OSError:
        return False
    return False


def _prune_stale_generated_runs(output_root: Path, active_run_ids: set[str]) -> None:
    """Remove only generated report caches that no longer exist in manifest."""
    runs_root = output_root / "runs"
    if not runs_root.exists():
        return
    for child in runs_root.iterdir():
        if child.is_dir() and child.name not in active_run_ids:
            shutil.rmtree(child)


def build_dashboard(log_root: Path, output_root: Path, selected: Sequence[Path] | None = None, rebuild_all: bool = False) -> list[dict[str, Any]]:
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "assets").mkdir(exist_ok=True)
    manifest_path = output_root / "manifest.json"
    previous: dict[str, dict[str, Any]] = {}
    if manifest_path.exists():
        try:
            old = json.loads(manifest_path.read_text(encoding="utf-8"))
            previous = {entry["source_relative"]: entry for entry in old.get("runs", [])}
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            previous = {}
    sources = (
        [path for path in selected if _is_reportable_source(path)]
        if selected
        else discover(log_root)
    )
    current_paths = {path.relative_to(log_root).as_posix() for path in discover(log_root)}
    results: dict[str, dict[str, Any]] = {key: value for key, value in previous.items() if key in current_paths}
    for source in sources:
        source = source.resolve()
        relative = source.relative_to(log_root.resolve()).as_posix()
        digest = _sha256(source)
        old = previous.get(relative)
        report_exists = bool(old and (output_root / "runs" / str(old.get("run_id")) / "report.html").exists())
        if old and old.get("source_hash") == digest and report_exists and not rebuild_all:
            results[relative] = old
            continue
        results[relative] = build_one(source, log_root.resolve(), output_root)
    runs = sorted(results.values(), key=lambda row: float(row.get("started_at", 0)), reverse=True)
    manifest = {"schema_version": SCHEMA_VERSION, "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "runs": runs}
    temporary_manifest = manifest_path.with_suffix(".json.tmp")
    temporary_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary_manifest, manifest_path)
    (output_root / "assets" / "style.css").write_text(_style(), encoding="utf-8")
    temporary_index = output_root / ".index.html.tmp"
    temporary_index.write_text(_index_html(runs), encoding="utf-8")
    os.replace(temporary_index, output_root / "index.html")
    temporary_compare = output_root / ".compare.html.tmp"
    temporary_compare.write_text(_compare_html(), encoding="utf-8")
    os.replace(temporary_compare, output_root / "compare.html")
    _prune_stale_generated_runs(
        output_root, {str(run["run_id"]) for run in runs}
    )
    return runs


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run", action="append", type=Path, default=[])
    parser.add_argument("--all", action="store_true", help="rebuild every report")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    selected = [path.resolve() for path in args.run] or None
    runs = build_dashboard(args.log_root.resolve(), args.output.resolve(), selected, rebuild_all=args.all)
    print(f"Dashboard ready: {args.output / 'index.html'} ({len(runs)} runs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

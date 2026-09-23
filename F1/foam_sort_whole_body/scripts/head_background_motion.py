"""Conservative background image-motion evidence for the F1 review queue.

Only numpy is needed for analyze_pair. Inputs are raw (no montage/text/border)
head-camera grayscale arrays, normally resized by the caller to 480 x 270,
with intensities in [0, 255]. Pixel units refer to the supplied array, not the
source JPEG. Positive displacement is right/down from previous to current.

This is NOT chassis odometry: camera/head motion, parallax and scene motion
remain possible. ``low_motion`` means reliable small image displacement, not
a stopped chassis. Bad registration always yields ``uncertain``. Results may
rank a human review queue; they do not authorize deletion, relabeling, mask
changes, or acceptance of a source. Fixed central ROIs reduce arm influence,
but cannot guarantee background segmentation for every camera pose.
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np


METHOD_VERSION = "central_roi_phase_ncc_v1"

# Six disjoint patches: omit both side arms and the bottom camera/robot rim.
# Fractions are (left, top, right, bottom). These are review heuristics, not
# calibrated physical thresholds. Their full settings are included in output.
ROI_FRACTIONS = (
    (0.29, 0.22, 0.49, 0.43),
    (0.50, 0.22, 0.70, 0.43),
    (0.29, 0.44, 0.49, 0.65),
    (0.50, 0.44, 0.70, 0.65),
    (0.29, 0.66, 0.49, 0.87),
    (0.50, 0.66, 0.70, 0.87),
)
THRESHOLDS = {
    "min_texture_std_255": 2.0,
    "min_gradient_rms_255": 0.7,
    "min_phase_psr": 7.0,
    "min_phase_peak_ratio": 1.25,
    "min_shifted_ncc": 0.50,
    "min_overlap_fraction": 0.50,
    "max_shift_fraction_of_roi": 0.40,
    "min_motion_px_at_width_480": 1.25,
    "max_low_motion_px_at_width_480": 0.75,
    "max_direction_difference_deg": 35.0,
    "min_motion_consensus_rois": 3,
    "min_low_motion_consensus_rois": 4,
    "min_consensus_fraction_of_valid": 0.60,
}


@lru_cache(maxsize=16)
def _window(height: int, width: int) -> np.ndarray:
    result = np.outer(np.hanning(height), np.hanning(width))
    result.setflags(write=False)
    return result


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    aa = a - a.mean()
    bb = b - b.mean()
    denominator = float(np.sqrt(np.sum(aa * aa) * np.sum(bb * bb)))
    return float(np.sum(aa * bb) / denominator) if denominator > 1e-9 else 0.0


def _aligned_ncc(a: np.ndarray, b: np.ndarray, dx: int, dy: int) -> tuple[float, float]:
    """Compare actual shared pixels, never circularly wrapped FFT pixels."""
    height, width = a.shape
    x0, x1 = max(0, -dx), min(width, width - dx)
    y0, y1 = max(0, -dy), min(height, height - dy)
    overlap = max(0, x1 - x0) * max(0, y1 - y0) / (height * width)
    if overlap < THRESHOLDS["min_overlap_fraction"]:
        return 0.0, overlap
    return _ncc(a[y0:y1, x0:x1], b[y0 + dy:y1 + dy, x0 + dx:x1 + dx]), overlap


def _quadratic_offset(left: float, center: float, right: float) -> float:
    curvature = left - 2 * center + right
    if curvature >= -1e-12:
        return 0.0
    return float(np.clip(0.5 * (left - right) / curvature, -0.5, 0.5))


def _analyze_roi(a: np.ndarray, b: np.ndarray, index: int, bounds: list[int]) -> dict:
    height, width = a.shape
    texture = min(float(a.std()), float(b.std()))
    gradient = min(
        float(np.sqrt(np.mean(np.diff(arr, axis=0) ** 2) + np.mean(np.diff(arr, axis=1) ** 2)))
        for arr in (a, b)
    )
    result = {
        "index": index, "row": index // 2, "column": index % 2,
        "bounds_xyxy": bounds, "valid": False, "reasons": [],
        "texture_std": texture, "gradient_rms": gradient,
        "dx_px": None, "dy_px": None, "displacement_px": None,
        "phase_psr": None, "phase_peak_ratio": None, "shifted_ncc": None,
        "zero_shift_ncc": None, "overlap_fraction": None, "confidence": 0.0,
    }
    if texture < THRESHOLDS["min_texture_std_255"] or gradient < THRESHOLDS["min_gradient_rms_255"]:
        result["reasons"].append("insufficient_texture")
        return result

    window = _window(height, width)
    fa = np.fft.rfft2((a - a.mean()) * window)
    fb = np.fft.rfft2((b - b.mean()) * window)
    cross = fb * fa.conj()
    magnitude = np.abs(cross)
    # Ignore essentially unsupported frequency bins (numerical noise).
    denominator = np.maximum(magnitude, max(float(magnitude.max()) * 1e-8, 1e-9))
    cross /= denominator
    cross[0, 0] = 0.0
    correlation = np.fft.irfft2(cross, s=a.shape)
    peak_y, peak_x = np.unravel_index(np.argmax(correlation), correlation.shape)
    peak = float(correlation[peak_y, peak_x])
    yy = np.minimum((np.arange(height) - peak_y) % height, (peak_y - np.arange(height)) % height)
    xx = np.minimum((np.arange(width) - peak_x) % width, (peak_x - np.arange(width)) % width)
    sidelobes = correlation[~((yy[:, None] <= 3) & (xx[None, :] <= 3))]
    psr = (peak - float(sidelobes.mean())) / max(float(sidelobes.std()), 1e-12)
    peak_ratio = peak / max(float(sidelobes.max()), 1e-12)
    int_dx = int(peak_x if peak_x <= width // 2 else peak_x - width)
    int_dy = int(peak_y if peak_y <= height // 2 else peak_y - height)
    dx = int_dx + _quadratic_offset(
        correlation[peak_y, (peak_x - 1) % width], peak, correlation[peak_y, (peak_x + 1) % width]
    )
    dy = int_dy + _quadratic_offset(
        correlation[(peak_y - 1) % height, peak_x], peak, correlation[(peak_y + 1) % height, peak_x]
    )
    shifted_ncc, overlap = _aligned_ncc(a, b, int_dx, int_dy)
    result.update({
        "dx_px": dx, "dy_px": dy, "displacement_px": math.hypot(dx, dy),
        "phase_psr": psr, "phase_peak_ratio": peak_ratio,
        "shifted_ncc": shifted_ncc, "zero_shift_ncc": _ncc(a, b),
        "overlap_fraction": overlap,
    })
    if psr < THRESHOLDS["min_phase_psr"]:
        result["reasons"].append("weak_phase_peak")
    if peak_ratio < THRESHOLDS["min_phase_peak_ratio"]:
        result["reasons"].append("ambiguous_phase_peak")
    if shifted_ncc < THRESHOLDS["min_shifted_ncc"]:
        result["reasons"].append("poor_shifted_ncc")
    if abs(dx) > width * THRESHOLDS["max_shift_fraction_of_roi"] or abs(dy) > height * THRESHOLDS["max_shift_fraction_of_roi"]:
        result["reasons"].append("shift_too_large_for_roi")
    if overlap < THRESHOLDS["min_overlap_fraction"]:
        result["reasons"].append("insufficient_overlap")
    result["valid"] = not result["reasons"]
    # A ranking score, not a calibrated probability of correctness.
    result["confidence"] = float(min(1.0, psr / 15.0) * max(0.0, shifted_ncc)) if result["valid"] else 0.0
    return result


def analyze_pair(prev_gray: np.ndarray, curr_gray: np.ndarray, dt_s: float) -> dict:
    """Return JSON-serializable, conservative background displacement evidence.

    Uncertain results have null top-level displacement/speed. Per-ROI tentative
    estimates are retained for debugging, along with their rejection reasons.
    ``confidence`` is only a ranking score, never a calibrated probability.
    """
    result = {
        "method_version": METHOD_VERSION, "status": "uncertain",
        "trusted_motion": False, "uncertain": True,
        "dx_px": None, "dy_px": None, "displacement_px": None, "speed_px_s": None,
        "confidence": 0.0, "valid_roi_count": 0, "consistent_roi_count": 0,
        "direction_consistency": None, "reasons": [], "rois": [],
        "dt_s": None, "image_size_wh": None,
        "thresholds": dict(THRESHOLDS),
        "units": "pixels_in_input_image_and_pixels_per_second_not_metric_velocity",
    }
    try:
        dt = float(dt_s)
        a = np.asarray(prev_gray, dtype=np.float64)
        b = np.asarray(curr_gray, dtype=np.float64)
    except (TypeError, ValueError, OverflowError):
        result["reasons"].append("invalid_input")
        return result
    if not math.isfinite(dt) or dt <= 0:
        result["reasons"].append("invalid_time_interval")
        return result
    result["dt_s"] = dt
    if a.ndim != 2 or b.shape != a.shape or min(a.shape) < 80:
        result["reasons"].append("invalid_grayscale_shape")
        return result
    height, width = a.shape
    result["image_size_wh"] = [width, height]
    if not np.isfinite(a).all() or not np.isfinite(b).all():
        result["reasons"].append("nonfinite_image")
        return result
    if a.min() < 0 or b.min() < 0 or a.max() > 255 or b.max() > 255:
        result["reasons"].append("intensities_outside_0_255")
        return result
    for index, (left, top, right, bottom) in enumerate(ROI_FRACTIONS):
        x0, y0, x1, y1 = round(left * width), round(top * height), round(right * width), round(bottom * height)
        result["rois"].append(_analyze_roi(a[y0:y1, x0:x1], b[y0:y1, x0:x1], index, [x0, y0, x1, y1]))

    valid = [roi for roi in result["rois"] if roi["valid"]]
    result["valid_roi_count"] = len(valid)
    if len(valid) < THRESHOLDS["min_motion_consensus_rois"]:
        result["reasons"].append("insufficient_reliable_rois")
        return result

    scale = width / 480.0
    motion_floor = THRESHOLDS["min_motion_px_at_width_480"] * scale
    low_ceiling = THRESHOLDS["max_low_motion_px_at_width_480"] * scale
    moving = [roi for roi in valid if roi["displacement_px"] >= motion_floor]
    quiet = [roi for roi in valid if roi["displacement_px"] <= low_ceiling]
    cosine_floor = math.cos(math.radians(THRESHOLDS["max_direction_difference_deg"]))

    best = []
    best_score = (-1, -1.0)
    for seed in moving:
        vector = np.array([seed["dx_px"], seed["dy_px"]]) / seed["displacement_px"]
        cluster = []
        for roi in moving:
            other = np.array([roi["dx_px"], roi["dy_px"]])
            direction_agrees = float(np.dot(vector, other) / roi["displacement_px"]) >= cosine_floor
            # Loose magnitude agreement allows perspective/depth variation,
            # while rejecting a tiny jitter vector aligned by coincidence.
            magnitude_agrees = abs(roi["displacement_px"] - seed["displacement_px"]) <= max(2.5 * scale, 0.75 * seed["displacement_px"])
            if direction_agrees and magnitude_agrees:
                cluster.append(roi)
        score = (len(cluster), sum(roi["confidence"] for roi in cluster))
        if score > best_score:
            best, best_score = cluster, score

    def enough_support(rois: list[dict], minimum: int) -> bool:
        return (
            len(rois) >= minimum
            and len(rois) / len(valid) >= THRESHOLDS["min_consensus_fraction_of_valid"]
            and len({roi["row"] for roi in rois}) >= 2
            and len({roi["column"] for roi in rois}) == 2
        )

    if enough_support(best, THRESHOLDS["min_motion_consensus_rois"]):
        consensus = best
        result.update(status="motion", trusted_motion=True, uncertain=False)
        vectors = np.array([[roi["dx_px"], roi["dy_px"]] for roi in consensus])
        unit_vectors = vectors / np.linalg.norm(vectors, axis=1)[:, None]
        result["direction_consistency"] = float(np.linalg.norm(unit_vectors.mean(axis=0)))
        result["reasons"].append("consistent_background_image_motion")
    elif enough_support(quiet, THRESHOLDS["min_low_motion_consensus_rois"]) and not moving:
        consensus = quiet
        result.update(status="low_motion", uncertain=False)
        result["reasons"].append("reliable_small_image_displacement_not_chassis_stop_proof")
    else:
        result["consistent_roi_count"] = max(len(best), len(quiet))
        result["reasons"].append("inconsistent_or_borderline_roi_motion")
        return result

    dx = float(np.median([roi["dx_px"] for roi in consensus]))
    dy = float(np.median([roi["dy_px"] for roi in consensus]))
    displacement = math.hypot(dx, dy)
    result.update(
        dx_px=dx, dy_px=dy, displacement_px=displacement, speed_px_s=displacement / dt,
        consistent_roi_count=len(consensus),
        confidence=float(np.median([roi["confidence"] for roi in consensus]) * len(consensus) / len(ROI_FRACTIONS)),
    )
    result["consensus_roi_indices"] = [roi["index"] for roi in consensus]
    return result

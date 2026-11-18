"""Robust planar edge direction, separate from trimming for a plane fit."""
import math

import numpy as np


# A small projected tail models isolated depth/segmentation returns. This is
# an estimator parameter, not a motion or grasp threshold. It does not tolerate
# large foreign regions, certify full-face visibility, or certify absolute yaw.
PROJECTION_TAIL = .01


def edge_direction(points, center, x_axis, y_axis):
    """Fit the observed face boundary modulo 90 degrees without rounding corners.

    Radial trimming helps the plane fit but removes the very corners needed for
    a minimum-area rectangle. Keep the selected planar support for yaw and use
    its 1st/99th projected percentiles instead of extrema. A fixed coarse/fine
    search avoids dependence on a noisy convex-hull edge. Work in angle batches
    so large, valid face clouds do not create an unbounded projection matrix.
    """
    uv = np.column_stack(((points - center) @ x_axis, (points - center) @ y_axis))

    def spans(angles):
        widths = []
        for begin in range(0, len(angles), 16):
            values = angles[begin:begin + 16]
            axes = np.stack((np.cos(values), np.sin(values)), axis=1)
            perpendicular = np.stack((-axes[:, 1], axes[:, 0]), axis=1)
            x = np.quantile(uv @ axes.T, [PROJECTION_TAIL, 1 - PROJECTION_TAIL], axis=0)
            y = np.quantile(uv @ perpendicular.T, [PROJECTION_TAIL, 1 - PROJECTION_TAIL], axis=0)
            widths.extend(np.column_stack((x[1] - x[0], y[1] - y[0])))
        return np.asarray(widths)

    coarse = np.deg2rad(np.arange(0., 90., 2.))
    coarse_spans = spans(coarse)
    best = coarse[np.argmin(np.prod(coarse_spans, axis=1))]
    fine = best + np.deg2rad(np.arange(-2., 2.00001, .1))
    fine_spans = spans(fine)
    index = int(np.argmin(np.prod(fine_spans, axis=1)))
    extent = fine_spans[index]
    if not np.isfinite(extent).all() or np.min(extent) < 1e-6:
        raise ValueError('degenerate block edge')
    angle = float(fine[index])
    direction = x_axis * math.cos(angle) + y_axis * math.sin(angle)
    return direction, dict(method='projected_quantile_rectangle', projection_tail=PROJECTION_TAIL,
        coarse_step_deg=2., fine_step_deg=.1, robust_planar_spans_m=extent.tolist(),
        observed_surface_only=True, absolute_yaw_certified=False)

"""Geometric endpoint advice, never an executable escape or grasp certificate."""
import numpy as np

from .contact_departure import _corners
from .geometry import transform, tool_sweep_clear


def endpoint_clearance_hint(scene, pose, obstacle):
    """Translate the whole tool hull outside one box at fixed orientation.

    The direction is expressed in object coordinates for continuous grasp
    proposals. It says nothing about jaw contact, IK, other obstacles or the
    connecting path. In particular, a colliding start pose must not execute
    this vector as an escape command.
    """
    if not scene.tool_local_boxes or obstacle.get('type') == 'halfspace':
        return None
    from .skills import tool_margin
    pose = transform(pose)
    other, other_axes = _corners(obstacle)
    points, directions = [], []
    for box in scene.tool_local_boxes:
        corners, axes = _corners(box)
        points.append(corners @ pose[:3, :3].T + pose[:3, 3])
        directions.append(axes @ pose[:3, :3].T)
    points = np.concatenate(points)
    tool_axes = np.concatenate(directions)
    axes = np.concatenate((tool_axes, other_axes,
        np.cross(tool_axes[:, None, :], other_axes[None, :, :]).reshape(-1, 3)))
    axes = axes[np.linalg.norm(axes, axis=1) > 1e-10]
    axes /= np.linalg.norm(axes, axis=1)[:, None]
    axes = np.concatenate((axes, -axes))
    # Match the original obstacle-frame padding cube, not a spherical margin.
    margin = tool_margin(scene)
    padding = margin * np.abs(axes @ other_axes.T).sum(axis=1)
    gaps = (points @ axes.T).min(0) - (other @ axes.T).max(0) - padding
    index = int(np.argmax(gaps))
    # One micrometre only avoids strict face-touch rounding in this advisory.
    distance = max(0., -float(gaps[index])) + 1e-6
    displacement = axes[index] * distance
    translated = pose.copy()
    translated[:3, 3] += displacement
    if not tool_sweep_clear(translated, translated, scene.eef_tip_reference,
            scene.attached_radius_m, (obstacle,), margin_m=margin,
            local_boxes=scene.tool_local_boxes):
        return None
    return dict(frame='object', translation_m=(transform(scene.base_object)[:3, :3].T @ displacement).tolist(),
        distance_m=distance, scope='single_obstacle_tool_endpoint_fixed_orientation',
        path_checked=False, grasp_checked=False, motion_authorized=False)

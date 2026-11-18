"""Select an observed planar face of a known upright object from a multi-face mask.

This is a geometric measurement filter, not a block-size prior or contact detector.
The configured world normal limits the supported task orientation explicitly.
"""
from __future__ import annotations

import math
import numpy as np

from .geometry import transform


def select_upright_face(points_camera, base_camera, config, *, diagnostics=None):
    """Return measured inlier points and diagnostics; ambiguous parallel faces fail."""
    if not isinstance(config, dict) or config.get('kind') != 'upright_planar_face':
        raise ValueError('upright_surface_selection_configuration_required')
    expected = np.asarray(config.get('normal_world'), float)
    scalar_names = ('normal_tolerance_deg', 'distance_threshold_m', 'minimum_inlier_fraction',
                    'minimum_planar_span_m', 'competing_support_ratio')
    values = [config.get(name) for name in scalar_names]
    if (expected.shape != (3,) or not np.isfinite(expected).all()
            or np.linalg.norm(expected) < 1e-8
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values)):
        raise ValueError('invalid_surface_selection_parameters')
    angle, threshold, fraction, span_min, competing_ratio = values
    minimum = config.get('minimum_points')
    if (isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 20
            or not 0 < angle < 45 or not 0 < threshold <= .01 or not 0 < fraction <= 1
            or not 0 < span_min or not 0 < competing_ratio <= 1):
        raise ValueError('invalid_surface_selection_parameters')
    expected /= np.linalg.norm(expected)
    points = np.asarray(points_camera, float)
    report={} if diagnostics is None else diagnostics
    report.pop('surface_anchor',None);report.pop('supporting_plane_candidate',None)
    report.update(kind='upright_planar_face',input_points=len(points) if points.ndim else 0,
        minimum_points=minimum,minimum_inlier_fraction=fraction,
        minimum_planar_span_m=span_min,competing_support_ratio=competing_ratio,
        distance_threshold_m=threshold,physical_accuracy_certified=False,
        full_object_visibility_certified=False,diagnostic_only=True,accepted=False,failed_checks=[])
    if points.ndim != 2 or points.shape[1:] != (3,) or len(points) < minimum or not np.isfinite(points).all():
        report['failed_checks']=['surface_points_missing_or_invalid']
        raise ValueError('surface_points_missing_or_invalid')
    t = transform(base_camera)
    world = points @ t[:3, :3].T + t[:3, 3]
    cosine = math.cos(math.radians(angle))

    def find_plane(cloud, seed):
        if len(cloud) < minimum:
            return None
        if config.get('pose_normal_prior')=='registered_upright':
            # A registered table-supported object has a known normal. Fit its
            # observed height instead of estimating tilt from a tiny noisy face.
            heights=np.sort(cloud@expected)
            ends=np.searchsorted(heights,heights+2*threshold,side='right')
            first=int(np.argmax(ends-np.arange(len(heights))))
            height=float(np.median(heights[first:ends[first]]))
            keep=np.abs(cloud@expected-height)<=threshold
            if keep.sum()<minimum:return None
            centre=cloud[keep].mean(0)
            return centre,expected.copy(),keep
        rng = np.random.default_rng(seed)
        sample = cloud[rng.choice(len(cloud), min(4096, len(cloud)), replace=False)]
        best = None
        count = 0
        for _ in range(256):
            a, b, c = sample[rng.choice(len(sample), 3, replace=False)]
            normal = np.cross(b - a, c - a)
            length = np.linalg.norm(normal)
            if length < 1e-10:
                continue
            normal /= length
            if abs(normal @ expected) < cosine:
                continue
            keep = np.abs((sample - a) @ normal) <= threshold
            if keep.sum() > count:
                count = int(keep.sum())
                best = a, normal
        if best is None:
            return None
        centre, normal = best
        # Refine using actual selected points; do not extend or resize the measured face.
        for _ in range(2):
            keep = np.abs((cloud - centre) @ normal) <= threshold
            if keep.sum() < minimum:
                return None
            centre = cloud[keep].mean(0)
            _, _, axes = np.linalg.svd(cloud[keep] - centre, full_matrices=False)
            normal = axes[-1]
            if normal @ expected < 0:
                normal = -normal
            if normal @ expected < cosine:
                return None
        keep = np.abs((cloud - centre) @ normal) <= threshold
        return centre, normal, keep

    candidate = find_plane(world, 17)
    if candidate is None:
        report['failed_checks']=['supported_upright_face_not_observed']
        raise ValueError('supported_upright_face_not_observed')
    centre, normal, selected = candidate
    selected_count = int(selected.sum())
    failed=[]
    if selected_count < minimum:
        failed.append('insufficient_upright_face_support')
    remaining = world[np.abs((world - centre) @ normal) > 2 * threshold]
    other = find_plane(remaining, 29)
    competing_count=0 if other is None else int(other[2].sum())
    if competing_count >= max(minimum, selected_count * competing_ratio):
        failed.append('ambiguous_parallel_object_faces')
    centred = world[selected] - centre
    if selected_count>=3:
        _, _, axes = np.linalg.svd(centred, full_matrices=False)
        plane_coordinates = centred @ axes[:2].T
        observed_spans = np.quantile(plane_coordinates, .95, axis=0) - np.quantile(plane_coordinates, .05, axis=0)
    else:observed_spans=np.zeros(2)
    if np.min(observed_spans) < span_min:
        failed.append('observed_face_is_too_narrow')
    residual = np.abs(centred @ normal)
    report.update(
        normal_basis='registered_upright_prior' if config.get('pose_normal_prior')=='registered_upright' else 'measured_plane',
        inlier_points=selected_count, inlier_fraction_of_mask_points=selected_count / len(points),
        absolute_support_sufficient=selected_count>=minimum,
        fractional_support_sufficient=selected_count/len(points)>=fraction,
        fraction_used_as_admission_gate=False,
        quality_warnings=[] if selected_count/len(points)>=fraction else ['planar_support_fraction_below_reference'],
        remaining_points=len(remaining),competing_inlier_points=competing_count,
        competing_plane_limit_points=max(minimum,selected_count*competing_ratio),
        normal_world=normal.tolist(), normal_error_deg=float(math.degrees(math.acos(float(np.clip(normal @ expected, -1, 1))))),
        residual_p95_m=float(np.quantile(residual, .95)) if len(residual) else None,
        observed_planar_spans_m=observed_spans.tolist(),
        failed_checks=failed,accepted=not failed)
    # A measured patch can locate conservative occupied volume even when its
    # area/span cannot support a full manipulation frame. Keep the original
    # absolute support, residual and competing-plane checks for these anchors.
    if (selected_count>=minimum and report['residual_p95_m'] is not None
            and report['residual_p95_m']<=threshold
            and competing_count<max(minimum,selected_count*competing_ratio)):
        report['surface_anchor']=dict(source='current_mask_planar_inliers',
            bounds_min=world[selected].min(0).tolist(),bounds_max=world[selected].max(0).tolist(),
            inlier_points=selected_count,minimum_points=minimum,normal_world=normal.tolist(),
            residual_p95_m=report['residual_p95_m'],distance_threshold_m=threshold,
            competing_plane_excluded=True,
            extremal_points_world=[world[selected][index].tolist() for axis in range(3)
                for index in (int(np.argmin(world[selected][:,axis])),int(np.argmax(world[selected][:,axis])))])
        # A prior-normal slice is not necessarily a physical exterior face.
        # Independently measure its normal, and retain ALL target depth points
        # as counter-evidence. The registered-solid-box consumer additionally
        # checks those points against its existing measurement margin.
        independent=axes[-1].copy()
        if independent@normal<0:independent=-independent
        difference=float(math.acos(float(np.clip(independent@normal,-1,1))))
        camera_distance=float((t[:3,3]-centre)@independent)
        actual_residual=float(np.quantile(np.abs(centred@independent),.95))
        if difference<=math.radians(angle) and camera_distance>0 and actual_residual<=threshold:
            report['supporting_plane_candidate']=dict(centre_world=centre.tolist(),
                normal_world=independent.tolist(),selected_normal_world=normal.tolist(),
                normal_difference_rad=difference,normal_tolerance_rad=math.radians(angle),
                camera_plane_signed_distance_m=camera_distance,
                all_target_front_side_max_m=float(np.max((world-centre)@independent)),
                residual_p95_m=actual_residual)
    if failed:raise ValueError(failed[0])
    return points[selected],report

"""Measure an occluded aperture on the same rigid lid from a second RGB view."""
from itertools import permutations

import cv2
import numpy as np

from .aperture_image import aperture_features
from .fixture_tracking import square_on_lid
from .geometry import crop_intrinsics, transform
from .perception import measurement_region


def _ellipse(ellipse):
    center, diameters, angle = ellipse
    theta = np.deg2rad(angle)
    rotation = np.array([[np.cos(theta), -np.sin(theta)], [np.sin(theta), np.cos(theta)]])
    basis = np.eye(3)
    basis[:2, :2] = rotation @ np.diag(np.asarray(diameters) / 2)
    basis[:2, 2] = center
    inverse = np.linalg.inv(basis)
    conic = inverse.T @ np.diag([1., 1., -1.]) @ inverse
    angles = np.linspace(0, 2 * np.pi, 40, endpoint=False)
    perimeter = basis @ np.stack((np.cos(angles), np.sin(angles), np.ones_like(angles)))
    return conic, perimeter


def _homogeneous(points):
    points = np.asarray(points, dtype=float)
    return np.column_stack((points, np.ones(len(points)))).T


def _map(homography, points):
    image = homography @ points
    if np.any(abs(image[2]) < 1e-8) or np.any(image[2] * image[2, 0] <= 0):
        return None
    return image / image[2]


def _lid_maps(left_triangle, head_triangle, left_circle, head_circle, tolerance_px):
    # In triangle barycentric coordinates any homography matching the three
    # vertices is diagonal. Their distances in the two conics fix its scales.
    source = _homogeneous(left_triangle['corners_px'])
    ls, perimeter = _ellipse(left_circle['ellipse'])
    hs, _ = _ellipse(head_circle['ellipse'])
    source_diag = np.diag(source.T @ ls @ source)
    if min(source_diag) <= 0 or abs(np.linalg.det(source)) < 1:
        return
    rays = np.linalg.solve(source, perimeter)
    for order in permutations(head_triangle['corners_px']):
        target = _homogeneous(order)
        target_diag = np.diag(target.T @ hs @ target)
        if min(target_diag) <= 0 or np.linalg.det(target) * np.linalg.det(source) <= 0:
            continue
        scales = np.sqrt(source_diag / target_diag)
        scales /= scales[2]
        def residuals(weights):
            mapped = _map(target * weights, rays)
            if mapped is None:
                return None
            gradient = hs @ mapped
            return np.sum(mapped * gradient, axis=0) / np.maximum(2 * np.linalg.norm(gradient[:2],axis=0), 1e-10)

        residual = residuals(scales)
        if residual is None or np.quantile(abs(residual), .9) > 2 * tolerance_px:
            continue
        # Three exact triangle correspondences leave two projective scales.
        # Refine them using the full observed circular rim; the acceptance
        # residual is still evaluated in the head image's original pixels.
        for _ in range(3):
            delta = 1e-4
            shifted = [residuals(scales + v) for v in np.eye(3)[:2] * delta]
            if any(v is None for v in shifted):
                break
            jacobian = np.column_stack([(v - residual) / delta for v in shifted])
            step = np.linalg.lstsq(jacobian, -residual, rcond=None)[0]
            proposed = scales + np.r_[step, 0.]
            improved = residuals(proposed) if min(proposed) > 0 else None
            if improved is None or np.linalg.norm(improved) >= np.linalg.norm(residual):
                break
            scales, residual = proposed, improved
        error = float(np.quantile(abs(residual), .9))
        if error <= tolerance_px:
            yield (target * scales) @ np.linalg.inv(source), error


def _circle_neighbours(triangle, circles):
    # All three shapes belong to a single local patch on the same lid.
    vertices = np.asarray(triangle['corners_px'])
    span = max(np.ptp(vertices, axis=0))
    return [c for c in circles if np.linalg.norm(np.subtract(c['center_px'], triangle['center_px'])) <= 3 * span]


def _registered_corners(left, head, fit_px):
    for ht in head['triangle']:
        for hc in _circle_neighbours(ht, head['circle']):
            for lt in left['triangle']:
                for lc in _circle_neighbours(lt, left['circle']):
                    for h, error in _lid_maps(lt, ht, lc, hc, fit_px):
                        for square in _circle_neighbours(lt, left['square']):
                            mapped = _map(h, _homogeneous(square['corners_px']))
                            if mapped is not None:
                                yield mapped[:2].T, ht, hc, error


def _measure_candidate(corners, triangle, circle, error, depth, head_camera, config, triangle_distance):
    k = crop_intrinsics(head_camera['k_rgb'], head_camera['roi'])
    camera_world = np.linalg.inv(transform(head_camera['base_camera']))
    hole_reference = transform(config['base_hole'])
    clearance = config['hole_clearance_m']
    height_margin = config['planning_margin_m']
    triangle_mask = np.zeros(depth.shape, np.uint8)
    circle_mask = np.zeros_like(triangle_mask)
    cv2.fillConvexPoly(triangle_mask, np.asarray(triangle['corners_px'], np.int32), 1)
    center, axes, angle = circle['ellipse']
    cv2.ellipse(circle_mask, (tuple(center), tuple(axes), angle), 1, -1)
    normal = camera_world[:3, :3] @ hole_reference[:3, 2]
    ring = np.zeros(depth.shape, bool)
    heights = []
    for mask in (triangle_mask, circle_mask):
        region = measurement_region(mask.astype(bool), dict(kind='outer_ring', inner_px=3, outer_px=8))
        yy, xx = np.nonzero(region & np.isfinite(depth) & (depth > 0))
        if len(xx) < 20:
            raise ValueError('cross_view_landmark_lid_depth_missing')
        rays = np.column_stack((xx, yy, np.ones(len(xx)))) @ np.linalg.inv(k).T
        elevations = (rays * depth[yy, xx, None]) @ normal
        median = np.median(elevations)
        heights.append(median)
        on_lid = abs(elevations - median) <= height_margin
        if np.count_nonzero(on_lid) < 20:
            raise ValueError('cross_view_landmark_lid_depth_missing')
        ring[yy[on_lid], xx[on_lid]] = True
    if abs(heights[0]-heights[1]) > height_margin:
        raise ValueError('cross_view_landmarks_on_different_surfaces')
    pose, details = square_on_lid(corners, depth, ring, head_camera, hole_reference)
    measured = np.asarray(details['aperture_sides_m'])
    dimensions = np.sort(config['cross_view_aperture']['inner_dimensions_m'])
    lengths = np.sort([(measured[0] + measured[2])/2, (measured[1] + measured[3])/2])
    if np.max(abs(dimensions - lengths)) > 2 * clearance:
        raise ValueError('cross_view_aperture_dimensions_disagree')
    # The separation of two landmarks on the lid survives rigid bucket motion.
    # A second quadrilateral in the left camera may be the rectangular slot.
    camera_pose = camera_world @ pose
    rays = np.linalg.solve(k, np.r_[triangle['center_px'], 1.])
    denominator = rays @ camera_pose[:3, 2]
    if abs(denominator) < .1:
        raise ValueError('cross_view_grazing_landmark')
    current_triangle = rays * ((camera_pose[:3, 3] @ camera_pose[:3, 2]) / denominator)
    spacing = np.linalg.norm(current_triangle - camera_pose[:3, 3])
    if abs(spacing - triangle_distance) > height_margin:
        raise ValueError('cross_view_fixture_landmark_spacing_disagrees')
    details.update(source='current_left_aperture_head_lid_rgbd', head_corners_px=corners.tolist(),
                   landmark_fit_px=error, registered_dimensions_m=dimensions.tolist(),
                   measured_triangle_to_square_m=float(spacing), registered_triangle_to_square_m=triangle_distance)
    return pose, details


def measure_aperture(left_rgb, head_rgb, depth, head_camera, reference_camera, config):
    """Register current views and identify the square by dimensions and landmark spacing."""
    k = crop_intrinsics(head_camera['k_rgb'], head_camera['roi'])
    nominal = transform(config['base_hole'])
    triangle = config['fixture_occlusion']['anchors'].get('bucket-triangle')
    if triangle is None:
        raise ValueError('cross_view_landmark_not_registered')
    old_triangle = np.linalg.solve(k, [*triangle['roi_center_px'], 1.]) * triangle['depth_m']
    triangle_distance = float(np.linalg.norm((transform(reference_camera) @ np.r_[old_triangle, 1.])[:3] - nominal[:3,3]))
    matches = []
    last_error = 'cross_view_aperture_not_registered'
    # Convert the metric allowance to pixels at the registered working distance;
    # current landmark positions and the fitted 3-D pose come from this sample.
    fit_px = min(k[0,0], k[1,1]) * config['hole_clearance_m'] / triangle['depth_m']
    for corners, ht, hc, error in _registered_corners(aperture_features(left_rgb), aperture_features(head_rgb), fit_px):
        try:
            measured = _measure_candidate(corners, ht, hc, error, depth, head_camera, config, triangle_distance)
        except ValueError as exc:
            last_error = str(exc)
            continue
        matches.append(measured)
    if len(matches) != 1:
        raise ValueError('cross_view_aperture_ambiguous' if matches else last_error)
    return matches[0]


def measure_observation(pipeline, observation, depth):
    from .images import decode_cached
    cfg = pipeline.config
    camera = cfg['hole_camera']
    frames = {(f.camera, f.stream):f for f in observation.frames}
    left = frames.get(('left','rgb'))
    head = frames.get((camera['camera'],'rgb'))
    head_depth = frames.get((camera['camera'],'depth'))
    if left is None or head is None or head_depth is None:
        raise ValueError('cross_view_frames_missing')
    if left.source_ns is None or left.frame_id != 'left_wrist_color_optical_frame' or not left.camera_info_sha256:
        raise ValueError('cross_view_left_frame_identity_missing')
    stamps = [frame.received_ns for frame in (left, head, head_depth)]
    if max(stamps) - min(stamps) > cfg['cross_view_aperture']['max_pair_skew_ns']:
        raise ValueError('cross_view_frames_not_contemporaneous')
    maps = pipeline.secondary_maps.get(camera['camera'])
    if maps is None:
        raise ValueError('cross_view_rectification_unavailable')
    head_rgb = decode_cached(head, pipeline.blob(head.blob_sha256))
    rectified = cv2.remap(head_rgb, *maps, interpolation=cv2.INTER_LINEAR)
    x0,y0,x1,y1 = camera['roi']
    result, diagnostics = measure_aperture(decode_cached(left, pipeline.blob(left.blob_sha256)),
        rectified[y0:y1,x0:x1], depth, camera, pipeline.fixture_reference_camera, cfg)
    diagnostics.update(left_rgb=left.blob_sha256, head_rgb=head.blob_sha256, head_depth=head_depth.blob_sha256,
                       receipt_skew_ns=max(stamps)-min(stamps))
    return result, diagnostics

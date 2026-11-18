"""Map a model's displayed-image hint to calibrated segmentation search pixels.

A hint only narrows a new segmentation search. These functions cannot establish
object pose, depth, visibility, task success, or permission to move. Issuance,
authority scope, acquisition identity and consumption are checked by the caller.
"""
from copy import deepcopy
from numbers import Real

import numpy as np


CAMERAS = frozenset(('head', 'right'))


def _vector(value, length, code):
    if (not isinstance(value, (list, tuple, np.ndarray)) or np.shape(value) != (length,)
            or any(isinstance(x, (bool, np.bool_)) or not isinstance(x, Real) for x in value)):
        raise ValueError(code)
    result = np.asarray(value, dtype=float)
    if not np.isfinite(result).all():
        raise ValueError(code)
    return result


def _size(value, code):
    result = _vector(value, 2, code)
    if np.any(result <= 0) or np.any(result != np.floor(result)):
        raise ValueError(code)
    return result.astype(np.int64)


def _rectangle(value, code):
    result = _vector(value, 4, code)
    if np.any(result[2:] <= result[:2]):
        raise ValueError(code)
    return result


def _roi(value, size, code):
    result = _rectangle(value, code)
    if (np.any(result != np.floor(result)) or np.any(result[:2] < 0)
            or np.any(result[2:] > size)):
        raise ValueError(code)
    return result.astype(np.int64)


def source_search_box(meta, box_normalized, *, points_normalized=None, point_labels=None):
    """Map normalized displayed-image bounds to full, unrectified RGB pixels.

    ``meta`` is the trusted receipt for the image actually shown to the model:
    ``frame``, ``observation_id``, ``roi`` (raw crop or None),
    ``model_image_size`` and ``source_image_size`` (both [width, height]).
    An optional ``camera`` must agree with frame.camera. Cropping occurs before
    resizing, matching EvidenceRemoteBackend.artifact; there is no letterbox.

    Returns the original source references plus ``box_source_px``. Pixel boxes
    describe half-open image regions, so normalized 1 maps to the outer edge.
    """
    if not isinstance(meta, dict):
        raise ValueError('target_hint_image_metadata_required')
    raw = meta.get('frame')
    if hasattr(raw, 'model_dump'):
        raw = raw.model_dump(mode='json')
    if not isinstance(raw, dict):
        raise ValueError('target_hint_source_frame_required')
    frame = deepcopy(raw)
    camera = meta.get('camera', frame.get('camera'))
    if camera not in CAMERAS or frame.get('camera') != camera or frame.get('stream') != 'rgb':
        raise ValueError('target_hint_rgb_camera_mismatch')
    observation = meta.get('observation_id')
    if not isinstance(observation, str) or not observation or len(observation) > 256:
        raise ValueError('target_hint_observation_identity_required')
    source_size = _size(meta.get('source_image_size'), 'target_hint_source_size_invalid')
    frame_size = _size([frame.get('width'), frame.get('height')], 'target_hint_frame_size_invalid')
    if not np.array_equal(source_size, frame_size):
        raise ValueError('target_hint_source_frame_size_mismatch')
    model_size = _size(meta.get('model_image_size'), 'target_hint_model_size_invalid')
    roi = (np.r_[np.zeros(2, dtype=int), source_size] if meta.get('roi') is None else
           _roi(meta['roi'], source_size, 'target_hint_display_roi_invalid'))
    bounds = _rectangle(box_normalized, 'target_hint_normalized_box_invalid')
    if np.any(bounds < 0.) or np.any(bounds > 1.):
        raise ValueError('target_hint_normalized_box_invalid')
    # Written as display->source to make the resize/crop contract explicit.
    displayed = bounds.reshape(2, 2) * model_size
    source = displayed * ((roi[2:] - roi[:2]) / model_size) + roi[:2]
    refs = {key: deepcopy(meta[key]) for key in (
        'image_id', 'image_sha256', 'source_blob_sha256', 'acquisition_instance',
        'host_boot_id', 'scope', 'calibration_id', 'received_ns') if key in meta}
    point_fields={}
    if points_normalized is not None:
        from .perception import validate_point_prompt
        points,labels=validate_point_prompt(dict(points=points_normalized,labels=point_labels),(1,1))
        point_fields=dict(points_source_px=(points*(roi[2:]-roi[:2])+roi[:2]).tolist(),point_labels=labels.tolist())
    elif point_labels is not None:raise ValueError('sam_points_invalid')
    return dict(refs, camera=camera, observation_id=observation, frame=frame,**point_fields,
                roi=None if meta.get('roi') is None else roi.tolist(),
                model_image_size=model_size.tolist(), source_image_size=source_size.tolist(),
                box_normalized=bounds.tolist(), box_source_px=source.ravel().tolist(),
                search_only=True, motion_evidence=False)


def rectified_search_box(source_hint, view_config):
    """Convert full raw RGB bounds to the mask provider's rectified ROI pixels.

    Uses cv2.undistortPoints with P=K, the inverse coordinate correspondence of
    scene_views.rectified_view's initUndistortRectifyMap(K,D,None,K), then crops
    the configured ROI. Sampling the perimeter includes distorted edge extrema
    that transforming just four corners can miss. The result is a conservative
    integer bounding box, clipped to that registered ROI, solely for search.
    """
    import cv2
    if not isinstance(source_hint, dict) or not isinstance(view_config, dict):
        raise ValueError('target_hint_mapping_configuration_required')
    camera = source_hint.get('camera')
    if camera not in CAMERAS or view_config.get('camera') != camera:
        raise ValueError('target_hint_view_camera_mismatch')
    size = _size(source_hint.get('source_image_size'), 'target_hint_source_size_invalid')
    roi = _roi(view_config.get('roi'), size, 'target_hint_registered_roi_invalid')
    bounds = _rectangle(source_hint.get('box_source_px'), 'target_hint_source_box_invalid')
    if np.any(bounds[:2] < 0) or np.any(bounds[2:] > size):
        raise ValueError('target_hint_source_box_invalid')
    try:
        k = np.asarray(view_config['k_rgb'], dtype=float)
        d = np.asarray(view_config['d_rgb'], dtype=float)
    except (KeyError, TypeError, ValueError):
        raise ValueError('target_hint_camera_parameters_invalid') from None
    if (k.shape != (3, 3) or not np.isfinite(k).all() or k[0, 0] <= 0 or k[1, 1] <= 0
            or not np.allclose(k[2], [0, 0, 1], rtol=0, atol=1e-12)
            or d.ndim != 1 or len(d) not in (0, 4, 5, 8, 12, 14) or not np.isfinite(d).all()):
        raise ValueError('target_hint_camera_parameters_invalid')
    frame = source_hint.get('frame') or {}
    if ('rgb_info_sha256' in view_config
            and frame.get('camera_info_sha256') != view_config['rgb_info_sha256']):
        raise ValueError('target_hint_camera_info_changed')
    t = np.linspace(0., 1., 65)
    low, high = bounds[:2], bounds[2:]
    x = low[0] + t * (high[0] - low[0])
    y = low[1] + t * (high[1] - low[1])
    perimeter = np.concatenate((np.c_[x, np.full_like(x, low[1])],
                                np.c_[x, np.full_like(x, high[1])],
                                np.c_[np.full_like(y, low[0]), y],
                                np.c_[np.full_like(y, high[0]), y]))
    try:
        pixels = cv2.undistortPoints(perimeter.reshape(-1, 1, 2), k, d, P=k).reshape(-1, 2)
    except cv2.error:
        raise ValueError('target_hint_rectification_failed') from None
    if not np.isfinite(pixels).all():
        raise ValueError('target_hint_rectification_failed')
    relative = pixels - roi[:2]
    extent = roi[2:] - roi[:2]
    left = np.maximum(relative.min(axis=0), 0.)
    right = np.minimum(relative.max(axis=0), extent)
    if np.any(right <= left):
        raise ValueError('target_hint_outside_registered_roi')
    clipped = (bool(np.any(relative.min(axis=0) < 0.) or np.any(relative.max(axis=0) > extent)))
    box = np.r_[np.floor(left), np.ceil(right)].astype(np.int64).tolist()
    extra={}
    if 'points_source_px' in source_hint:
        from .perception import validate_point_prompt
        points,labels=validate_point_prompt(dict(points=source_hint['points_source_px'],labels=source_hint['point_labels']),size[::-1])
        rectified=cv2.undistortPoints(points.reshape(-1,1,2),k,d,P=k).reshape(-1,2)-roi[:2]
        points,labels=validate_point_prompt(dict(points=rectified,labels=labels),extent[::-1])
        extra['point_prompt']=dict(points=points.tolist(),labels=labels.tolist())
    return dict(deepcopy(source_hint), box=box, registered_roi=roi.tolist(),**extra,
                rectified_roi_size=extent.tolist(), clipped_to_roi=clipped,
                search_only=True, motion_evidence=False, perimeter_samples=len(perimeter))

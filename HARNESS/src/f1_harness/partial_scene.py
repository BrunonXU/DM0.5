"""Current depth evidence for looking again when an object frame cannot be fitted.

Surface points are not an object pose. Unseen object volume is expanded using
the registered maximum body diameter, and holes in depth remain unobserved.
"""
import base64
from copy import deepcopy
import hashlib
import zlib

import numpy as np

from .contracts import FreeSpaceView, LocalCollisionEvidence
from .geometry import crop_intrinsics, masked_points, transform


def registered_box_surface_anchor(surface_diagnostics, planning_margin_m):
    """An independently measured exterior plane can bound a known solid box.

    This adds no object pose. A narrow face may still supply such a plane, but
    prior-normal slices, competing planes and contradicting depth cannot.
    """
    anchor=deepcopy(surface_diagnostics.get('surface_anchor'))
    if anchor is None:return None
    candidate=surface_diagnostics.get('supporting_plane_candidate')
    if candidate is not None and candidate['all_target_front_side_max_m']<=planning_margin_m:
        anchor['supporting_plane']=deepcopy(candidate)
    return anchor


def observed_target_bounds(mask, depth, view_config, camera_pose, registered_box):
    x0,y0,x1,y1=view_config['roi']
    points=masked_points(depth[y0:y1,x0:x1],mask,crop_intrinsics(view_config['k_rgb'],view_config['roi']))
    pose=transform(camera_pose);world=points@pose[:3,:3].T+pose[:3,3]
    size=np.asarray(registered_box['max'],float)-np.asarray(registered_box['min'],float)
    if size.shape!=(3,) or not np.isfinite(size).all() or np.any(size<=0):
        raise ValueError('registered_object_extent_required_for_partial_observation')
    return dict(observed_surface_min=world.min(axis=0).tolist(),
        observed_surface_max=world.max(axis=0).tolist(),
        registered_object_diagonal_m=float(np.linalg.norm(size)),
        depth_points=len(points),mask_pixels=int(np.asarray(mask,bool).sum()))


def observed_free_space_view(depth, view_config, camera_pose, frames, *, tile_size_px=16):
    """A tile is free up to its nearest depth only when every pixel has depth."""
    rgb=next(f for f in frames if f.camera==view_config['camera'] and f.stream=='rgb')
    dep=next(f for f in frames if f.camera==view_config['camera'] and f.stream=='depth')
    depth=np.asarray(depth,float)
    if depth.shape!=(rgb.height,rgb.width):raise ValueError('partial_depth_pixels_must_be_rectified_full_rgb')
    valid=np.isfinite(depth)&(depth>0)
    if not valid.any():raise ValueError('partial_free_space_depth_unavailable')
    conservative=np.zeros(depth.shape,dtype='<f4')
    conservative[valid]=np.nextafter(depth[valid].astype('<f4'),np.float32(-np.inf))
    conservative[conservative<=0]=0
    grid=np.zeros(((rgb.height+tile_size_px-1)//tile_size_px,(rgb.width+tile_size_px-1)//tile_size_px))
    for gy,y in enumerate(range(0,rgb.height,tile_size_px)):
        for gx,x in enumerate(range(0,rgb.width,tile_size_px)):
            tile=depth[y:y+tile_size_px,x:x+tile_size_px]
            if valid[y:y+tile_size_px,x:x+tile_size_px].all():grid[gy,gx]=tile.min()
    # A valid first-surface return establishes the positive optical ray in
    # front of it; the closest surface in another pixel is not a near clip.
    # The planner excludes z<=0 and subtracts the existing depth margin once.
    return FreeSpaceView(camera=view_config['camera'],camera_pose_world=tuple(transform(camera_pose).ravel()),
        intrinsics=tuple(np.asarray(view_config['k_rgb'],float).ravel()),image_width=rgb.width,image_height=rgb.height,
        tile_size_px=tile_size_px,grid_width=grid.shape[1],grid_height=grid.shape[0],
        min_depth_m=tuple(grid.ravel()),near_depth_m=0.,
        source_rgb_sha256=rgb.blob_sha256,source_depth_sha256=dep.blob_sha256,
        fine_depth_zlib_base64=base64.b64encode(zlib.compress(conservative.tobytes(),level=1)).decode('ascii'),
        fine_depth_sha256=hashlib.sha256(conservative.tobytes()).hexdigest())


def local_collision_evidence(observation, config, target_bounds, free_views):
    radius=target_bounds['registered_object_diagonal_m']+config['planning_margin_m']
    anchor=target_bounds.get('surface_anchor')
    if anchor is None:
        low=np.asarray(target_bounds['observed_surface_min'])-radius
        high=np.asarray(target_bounds['observed_surface_max'])+radius
    else:
        # Every body point is within the registered diameter of EVERY observed
        # object anchor. Intersect those bounds, including measurement margin.
        low=np.asarray(anchor['bounds_max'])-radius
        high=np.asarray(anchor['bounds_min'])+radius
    cameras={view.camera for view in free_views}
    return LocalCollisionEvidence(object_id=config['object_id'],frame_id=config['native_frame'],
        observation_id=observation.observation_id,calibration_id=config['calibration_id'],
        measured_at_ns=observation.received_ns,state_received_ns=observation.state_received_ns,
        source_frames=tuple(f for f in observation.frames if f.camera in cameras),
        **target_bounds,
        possible_occupancy_min=tuple(low),possible_occupancy_max=tuple(high),
        planning_margin_m=config['planning_margin_m'],free_space_views=tuple(free_views))

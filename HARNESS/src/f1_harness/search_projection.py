"""Project a previous measured object into a new camera pose as a search hint only."""
from itertools import product
import numpy as np
from .geometry import transform, crop_intrinsics


def object_search_box(base_object, base_camera, local_box, camera, *, margin_m):
    low,high=np.asarray(local_box['min'],float),np.asarray(local_box['max'],float)
    if (low.shape!=(3,) or high.shape!=(3,) or not np.isfinite([low,high]).all()
            or np.any(low>=high) or not np.isfinite(margin_m) or margin_m<0):
        raise ValueError('invalid_search_object_geometry')
    corners=np.array(list(product(*zip(low-margin_m,high+margin_m))))
    pose=np.linalg.inv(transform(base_camera))@transform(base_object)
    points=corners@pose[:3,:3].T+pose[:3,3]
    if np.any(points[:,2]<=0):return None
    pixels=points@crop_intrinsics(camera['k_rgb'],camera['roi']).T
    pixels=pixels[:,:2]/pixels[:,2:]
    width,height=np.asarray(camera['roi'][2:])-camera['roi'][:2]
    lo=np.maximum(0,np.floor(pixels.min(0))).astype(int)
    hi=np.minimum([width,height],np.ceil(pixels.max(0))).astype(int)
    if np.any(hi-lo<3):return None
    return [*lo.tolist(),*hi.tolist()]

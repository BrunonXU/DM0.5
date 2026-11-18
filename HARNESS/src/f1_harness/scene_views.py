"""Calibrated secondary camera view; no motion or success inference."""
import numpy as np
from .geometry import register_depth
from .images import decode_cached as decode


def validate_secondary_identity(observation,config):
    """Check present frame identities even when pairing quality is unavailable."""
    camera=config['camera']
    frames={f.stream:f for f in observation.frames if f.camera==camera}
    pair=next((p for p in observation.pairings if p.camera==camera),None)
    for stream,frame in frames.items():
        if (frame.source_ns is None or frame.camera_info_sha256!=config[stream+'_info_sha256']
                or stream=='depth' and frame.depth_scale_m!=config['depth_scale_m']):
            raise ValueError('secondary_camera_identity_changed')
    if pair is not None and 'rgb' in frames and 'depth' in frames:
        if pair.rgb_sequence!=frames['rgb'].sequence or pair.depth_sequence!=frames['depth'].sequence:
            raise ValueError('secondary_camera_identity_changed')


def rectified_view(observation, config, blob, cache, *, allow_receipt=False):
    import cv2
    camera=config['camera']
    validate_secondary_identity(observation,config)
    pair=next((p for p in observation.pairings if p.camera==camera),None)
    allowed=('source_time_bounded','source_metadata_matched','receipt_only') if allow_receipt else (
        'source_time_bounded','source_metadata_matched')
    if pair is None or pair.quality not in allowed:raise ValueError('secondary_camera_pairing_unavailable')
    frames={f.stream:f for f in observation.frames if f.camera==camera}
    rgb,depth=frames['rgb'],frames['depth']
    if (pair.rgb_sequence!=rgb.sequence or pair.depth_sequence!=depth.sequence
            or rgb.source_ns is None or depth.source_ns is None
            or rgb.camera_info_sha256!=config['rgb_info_sha256']
            or depth.camera_info_sha256!=config['depth_info_sha256']
            or depth.depth_scale_m!=config['depth_scale_m']):
        raise ValueError('secondary_camera_identity_changed')
    pixels=decode(rgb,blob(rgb.blob_sha256));z=decode(depth,blob(depth.blob_sha256))
    k=np.asarray(config['k_rgb'],float);size=(rgb.width,rgb.height)
    aligned=register_depth(z,config['k_depth'],config['d_depth'],config['rgb_depth'],
                           k,config['d_rgb'],size,config['depth_scale_m'])
    if camera not in cache:
        cache[camera]=cv2.initUndistortRectifyMap(k,np.asarray(config['d_rgb'],float),None,k,size,cv2.CV_32FC1)
    pixels=cv2.remap(pixels,*cache[camera],interpolation=cv2.INTER_LINEAR)
    aligned=cv2.remap(aligned,*cache[camera],interpolation=cv2.INTER_NEAREST)
    x0,y0,x1,y1=config['roi']
    return pixels[y0:y1,x0:x1],aligned[y0:y1,x0:x1],(rgb.blob_sha256,depth.blob_sha256)

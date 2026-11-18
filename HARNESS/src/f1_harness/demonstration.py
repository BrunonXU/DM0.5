"""Retarget measured demonstration shape, with current geometric endpoints.

Old joints are soft IK preferences, never executable references. Old absolute
object locations, contact precision and gripper percentages are not transferable.
"""
import numpy as np
from scipy.spatial.transform import Rotation, Slerp
from .geometry import transform


def validate_template(value):
    if (not isinstance(value,dict) or value.get('schema')!='f1-success-trajectory-v1'
            or not value.get('source_run') or not value.get('source_sha256')
            or value.get('measurement_kind')!='measured_feedback'):
        raise ValueError('invalid_success_trajectory_provenance')
    for name,part in value['segments'].items():
        if name not in ('approach','grasp','lift','transfer','withdraw'):
            raise ValueError('invalid_success_trajectory_segment')
        poses=np.asarray(part['eef_poses'],float);q=np.asarray(part['arm_deg'],float)
        times=np.asarray(part['times_s'],float)
        if (not 2<=len(poses)<=16 or poses.shape!=(len(poses),4,4) or q.shape!=(len(poses),7)
                or times.shape!=(len(poses),) or not np.isfinite(q).all()
                or not np.isfinite(times).all() or np.any(np.diff(times)<=0)):
            raise ValueError('invalid_success_trajectory_samples')
        for p in poses:transform(p)
    return value


def retarget(segment,start,end,*,preserve_rotation=True):
    """Preserve the demonstrated curve residual, anchored to both live ends."""
    poses=np.asarray(segment['eef_poses'],float);q=np.asarray(segment['arm_deg'],float)
    t=np.asarray(segment['times_s'],float);u=(t-t[0])/(t[-1]-t[0])
    start,end=transform(start),transform(end)
    old_line=Slerp([0,1],Rotation.from_matrix(poses[[0,-1],:3,:3]))(u).as_matrix()
    new_line=Slerp([0,1],Rotation.from_matrix(np.stack([start[:3,:3],end[:3,:3]])))(u).as_matrix()
    mapping=start[:3,:3]@poses[0,:3,:3].T
    scale=min(1.,np.linalg.norm(end[:3,3]-start[:3,3])/max(np.linalg.norm(poses[-1,:3,3]-poses[0,:3,3]),1e-9))
    result=[]
    for i,f in enumerate(u[1:],1):
        goal=np.eye(4)
        residual=poses[i,:3,3]-((1-f)*poses[0,:3,3]+f*poses[-1,:3,3])
        goal[:3,3]=(1-f)*start[:3,3]+f*end[:3,3]+scale*(mapping@residual)
        goal[:3,:3]=new_line[i]@old_line[i].T@poses[i,:3,:3] if preserve_rotation else new_line[i]
        result.append(goal)
    result[-1]=end.copy()
    return result,q[1:].tolist()

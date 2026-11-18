"""Measured body/head FK with the existing rigid camera hand-eye.

Only the URDF chain from world to head_Link3 is evaluated. No robot commands,
new calibration fit, joint-zero change or pose extrapolation is performed.
"""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np

from .geometry import transform

NAMES = tuple('head_j'+str(i) for i in range(1,4))+tuple('body_j'+str(i) for i in range(1,6))
MODEL = {**{'head_joint'+str(i):'head_j'+str(i) for i in range(1,4)},
         **{'body_joint'+str(6-i):'body_j'+str(i) for i in range(1,6)}}


def _rotation(axis, angle):
    axis=np.asarray(axis,float);norm=np.linalg.norm(axis)
    if not np.isfinite(norm) or norm<=0:raise ValueError('head_joint_axis_invalid')
    x,y,z=axis/norm;skew=np.array([[0,-z,y],[z,0,-x],[-y,x,0]])
    return np.eye(3)+np.sin(angle)*skew+(1-np.cos(angle))*(skew@skew)


class HeadKinematics:
    def __init__(self, spec):
        if spec.get('schema')!='f1-measured-head-v1':raise ValueError('head_kinematics_schema')
        files={}
        for key in ('urdf','mapping'):
            path=Path(spec[key+'_path']);data=path.read_bytes()
            if hashlib.sha256(data).hexdigest()!=spec[key+'_sha256']:
                raise ValueError('head_kinematics_asset_changed:'+key)
            files[key]=data
        self.handeye=transform(spec['T_head_Link3_camera'])
        self.signs=json.loads(files['mapping']).get('joint_signs',{})
        joints={j.find('child').attrib['link']:j for j in ET.fromstring(files['urdf']).findall('joint')}
        chain=[];link='head_Link3';visited=set()
        # Pinocchio's fixed-base world is the URDF root base_link.
        while link!='base_link':
            if link in visited or link not in joints:raise ValueError('head_world_chain_missing')
            visited.add(link);joint=joints[link];kind=joint.attrib['type'];name=joint.attrib['name']
            if kind not in ('fixed','revolute','continuous') or (kind!='fixed' and name not in MODEL):
                raise ValueError('head_world_chain_unsupported_joint:'+name)
            origin=joint.find('origin');attrs={} if origin is None else origin.attrib
            xyz=np.fromstring(attrs.get('xyz','0 0 0'),sep=' ')
            rpy=np.fromstring(attrs.get('rpy','0 0 0'),sep=' ')
            if xyz.shape!=(3,) or rpy.shape!=(3,) or not np.isfinite([xyz,rpy]).all():
                raise ValueError('head_joint_origin_invalid')
            pose=np.eye(4);pose[:3,3]=xyz
            pose[:3,:3]=_rotation([0,0,1],rpy[2])@_rotation([0,1,0],rpy[1])@_rotation([1,0,0],rpy[0])
            axis=joint.find('axis')
            direction=np.fromstring('1 0 0' if axis is None else axis.attrib['xyz'],sep=' ')
            chain.append((pose,kind,name,direction));link=joint.find('parent').attrib['link']
        self.chain=tuple(reversed(chain))
        if {MODEL[name] for _,kind,name,_ in self.chain if kind!='fixed'}!=set(NAMES):
            raise ValueError('head_world_chain_joint_mapping_mismatch')

    def pose(self, values):
        if not isinstance(values,dict):
            full=np.asarray(values,float)
            if full.shape!=(22,) or not np.isfinite(full).all():raise ValueError('head_full22_required')
            values=dict(zip(NAMES,full[14:]))
        if set(values)!=set(NAMES) or any(isinstance(v,bool) or not np.isfinite(v) for v in values.values()):
            raise ValueError('head_measured_posture_required')
        result=np.eye(4)
        for origin,kind,name,axis in self.chain:
            result=result@origin
            if kind!='fixed':
                hal=MODEL[name];motion=np.eye(4)
                motion[:3,:3]=_rotation(axis,np.deg2rad(values[hal]*self.signs.get(hal,1)))
                result=result@motion
        return transform(result@self.handeye)


def checked_head_model(scene, calibration):
    """A flag alone cannot bypass fixed-reference checks; bind the actual model."""
    spec=scene.get('head_kinematics')
    enabled=scene.get('geometry',{}).get('measured_head_pose') is True
    if spec is None and not enabled:return None
    if not isinstance(spec,dict) or not enabled:raise ValueError('head_kinematics_configuration_incomplete')
    identity=calibration['executor_identity'];head=calibration['cameras']['head']
    if any(spec.get(k+'_sha256')!=identity.get(k+'_sha256') for k in ('urdf','mapping')):
        raise ValueError('head_kinematics_calibration_model_mismatch')
    if not np.array_equal(transform(spec['T_head_Link3_camera']),transform(head['T_head_Link3_camera'])):
        raise ValueError('head_kinematics_handeye_mismatch')
    model=HeadKinematics(spec)
    if not np.allclose(model.pose(calibration['body_head_reference_deg']),transform(head['T_world_camera']),atol=1e-5,rtol=0):
        raise ValueError('head_kinematics_original_reference_mismatch')
    return model

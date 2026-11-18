"""Explicit, independently verified conversion between calibration and IK frame conventions."""
from dataclasses import dataclass
import numpy as np
from .geometry import transform


@dataclass(frozen=True)
class FrameChain:
    world_native: np.ndarray
    ik_eef_native_eef: np.ndarray
    native_frame: str
    ik_world_frame: str

    @classmethod
    def load(cls,record):
        if isinstance(record,dict) and record.get('admission_profile') == 'development_coarse':
            from .development import load_development_frame
            load_development_frame(record)
            return cls(transform(record['T_ik_world_native']),transform(record['T_ik_eef_native_eef']),
                       record['native_frame'],record['ik_world_frame'])
        if (not isinstance(record,dict) or record.get('accepted') is not True or record.get('synthetic') is not False
                or not record.get('evidence_ids') or not record.get('holdout_hash')):
            raise ValueError('independently_verified_two_sided_frame_chain_required')
        for key in ('native_frame','ik_world_frame','native_eef_frame','ik_eef_frame'):
            if not isinstance(record.get(key),str) or not record[key]:raise ValueError('explicit_frame_identity_required')
        return cls(transform(record['T_ik_world_native']),transform(record['T_ik_eef_native_eef']),
                   record['native_frame'],record['ik_world_frame'])

    def native_eef(self,world_ik_eef):
        return np.linalg.inv(self.world_native)@transform(world_ik_eef)@self.ik_eef_native_eef

    def world_pose(self,native_pose):return self.world_native@transform(native_pose)

    def ik_eef_object(self,native_eef_object):return self.ik_eef_native_eef@transform(native_eef_object)

    def object_ik_eef(self,object_native_eef):return transform(object_native_eef)@np.linalg.inv(self.ik_eef_native_eef)

    def world_obstacles(self,obstacles):
        from itertools import product
        result=[]
        for box in obstacles:
            if box.get('type')=='halfspace':
                point=np.asarray(box['point'],float);normal=np.asarray(box['normal'],float)
                if (point.shape!=(3,) or normal.shape!=(3,) or not np.isfinite([point,normal]).all()
                        or not np.isclose(np.linalg.norm(normal),1.,atol=1e-5)):
                    raise ValueError('invalid_native_halfspace')
                result.append(dict(box,point=(self.world_native[:3,:3]@point+self.world_native[:3,3]).tolist(),
                                   normal=(self.world_native[:3,:3]@normal).tolist()))
                continue
            low,high=np.asarray(box['min'],float),np.asarray(box['max'],float)
            if low.shape!=(3,) or high.shape!=(3,) or not np.isfinite([low,high]).all() or np.any(low>high):
                raise ValueError('invalid_native_obstacle')
            if 'pose' in box:
                result.append(dict(box,pose=(self.world_native@transform(box['pose'])).tolist()))
                continue
            corners=np.array(list(product(*zip(low,high))))
            points=corners@self.world_native[:3,:3].T+self.world_native[:3,3]
            result.append(dict(box,min=points.min(0).tolist(),max=points.max(0).tolist()))
        return result

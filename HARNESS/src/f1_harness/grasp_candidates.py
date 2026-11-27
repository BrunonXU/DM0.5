"""Registered pitch alternatives at the same capture point and jaw-closing axis.

These descriptors select geometry, never joint commands or permission to move.
The nominal four square symmetries remain separate candidate variants.
"""
from dataclasses import replace
import math

import numpy as np

from .contracts import HarnessError
from .geometry import transform


def registered_offsets(scene):
    """A fixture opts into a finite set; no extension is enabled by default."""
    values=scene.grasp_pitch_offsets_deg
    if (not isinstance(values,(list,tuple)) or len(values)>6
            or any(type(v) not in (int,float) or not math.isfinite(v)
                   or not 0<abs(v)<=45 for v in values)
            or len(set(values))!=len(values)):
        raise HarnessError('invalid_registered_grasp_pitch_offsets')
    return tuple(float(v) for v in values)


def descriptors(scene):
    """Prefer nominal grasp unless the fixture prioritizes registered pitches."""
    offsets=registered_offsets(scene)
    order=(*offsets,0.) if getattr(scene,'prefer_registered_grasp_pitch',False) else (0.,*offsets)
    return tuple(dict(strategy='tcp_pitch',pitch_offset_deg=value)
                 for value in order)


def validate_descriptor(scene, descriptor):
    if isinstance(descriptor,dict) and descriptor.get('strategy')=='object_tcp':
        from .contracts import MotionParameters
        from .motion_goals import grasp_descriptor
        if not scene.parameterized_grasp:raise HarnessError('parameterized_grasp_not_enabled')
        values={k:v for k,v in descriptor.items() if k!='strategy'}
        if set(values)-{'grasp_tcp_pose','approach_direction_object','approach_distance_m'}:
            raise HarnessError('invalid_grasp_candidate_descriptor')
        p=MotionParameters.model_validate(values).for_skill('approach_grasp')
        result=grasp_descriptor(p)
        if result is None:raise HarnessError('empty_grasp_geometry')
        return result
    if (not isinstance(descriptor,dict)
            or set(descriptor)!={'strategy','pitch_offset_deg'}
            or descriptor.get('strategy')!='tcp_pitch'):
        raise HarnessError('invalid_grasp_candidate_descriptor')
    value=descriptor['pitch_offset_deg']
    if (type(value) not in (int,float) or not math.isfinite(value)
            or (value not in (0.,*registered_offsets(scene))
                and not (scene.parameterized_grasp is True and abs(value)<=45))):
        raise HarnessError('unregistered_grasp_pitch_offset')
    return dict(strategy='tcp_pitch',pitch_offset_deg=float(value))


def pitched(scene):
    descriptor=scene.grasp_candidate_descriptor
    return (descriptor is not None
            and (descriptor.get('strategy')=='object_tcp'
                 or validate_descriptor(scene,descriptor)['pitch_offset_deg']!=0.))


def apply_descriptor(scene, descriptor):
    """Return new geometry, preserving nominal local X and the exact TCP point.

    Rotation is about the nominal Link7/local-X direction used by the registered
    grasp convention. This preserves that closing-axis convention; it is not a
    new calibration of the physical jaws. Rebuild from the nominal scene when
    choosing a different descriptor, so successive applications cannot drift.
    """
    selected=validate_descriptor(scene,descriptor)
    if scene.grasp_candidate_descriptor is not None:
        if validate_descriptor(scene,scene.grasp_candidate_descriptor)==selected:
            return scene
        raise HarnessError('grasp_candidate_already_applied')
    if selected['strategy']=='object_tcp':
        nominal=transform(scene.object_grasp_eef)
        if selected['grasp_tcp_pose'] is not None:
            nominal=transform(selected['grasp_tcp_pose'])@np.linalg.inv(transform(scene.eef_tip_reference))
        distance=selected['approach_distance_m']
        return replace(scene,object_grasp_eef=nominal,grasp_candidate_descriptor=selected,
            approach_direction_object=selected['approach_direction_object'],
            pregrasp_distances_m=scene.pregrasp_distances_m if distance is None else (distance,),
            grasp_height_policy='registered_point')
    angle=selected['pitch_offset_deg']
    if angle==0.:
        return replace(scene,grasp_candidate_descriptor=selected)
    if scene.eef_tip_reference is None:
        raise HarnessError('grasp_pitch_requires_registered_tcp')
    nominal=transform(scene.object_grasp_eef)
    tip=transform(scene.eef_tip_reference)
    point=(nominal@tip)[:3,3].copy()
    radians=math.radians(angle);c,s=math.cos(radians),math.sin(radians)
    rotation=np.array([[1.,0.,0.],[0.,c,-s],[0.,s,c]])
    changed=nominal.copy()
    changed[:3,:3]=nominal[:3,:3]@rotation
    changed[:3,3]=point-changed[:3,:3]@tip[:3,3]
    direction=getattr(scene,'pitched_grasp_approach_direction','tool_axis_horizontal')
    if direction not in ('tool_axis_horizontal','vertical'):
        raise HarnessError('invalid_pitched_grasp_approach_direction')
    return replace(scene,object_grasp_eef=changed,approach_direction=direction,
                   grasp_candidate_descriptor=selected)

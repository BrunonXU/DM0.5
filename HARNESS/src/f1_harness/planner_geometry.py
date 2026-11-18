"""Planner geometry receipts; raw depth grids stay local to collision checking."""
from copy import deepcopy
import json
import math


def has_local_planning_context(observation,geometry=None):
    """Identify partial scene summaries; this does not grant motion authority."""
    scope=observation.get('geometry_scope') if isinstance(observation,dict) else None
    return (scope=='coarse_local_environment' or scope=='coarse_object_verification'
        and isinstance(geometry,dict) and isinstance(geometry.get('local_environment'),dict))


def observation_warnings(values):
    """Quality advice stays visible and bounded; it grants or removes no authority."""
    if not isinstance(values,(list,tuple)):return []
    return list(dict.fromkeys(value[:200] for value in values if isinstance(value,str)))[:8]


def _without_depth_grids(value):
    if isinstance(value,dict):
        return {k:_without_depth_grids(v) for k,v in value.items()
                if k not in ('local_collision_evidence','local_collision_evidence_ref','free_space_views','min_depth_m',
                             'fine_depth_zlib_base64')}
    if isinstance(value,(list,tuple)):return [_without_depth_grids(v) for v in value]
    return deepcopy(value)


def pack_geometry(geometry):
    if not isinstance(geometry,dict):
        return geometry
    packed=_without_depth_grids(geometry)
    frames={}
    for obstacle in packed.get('obstacles') or []:
        if 'pose' not in obstacle:
            continue
        pose=obstacle.pop('pose')
        identity=next((name for name,value in frames.items() if value==pose),None)
        if identity is None:
            identity='frame_'+str(len(frames));frames[identity]=pose
        obstacle['pose_ref']=identity
    if frames:
        packed['obstacle_pose_frames']=frames
        packed['obstacle_pose_encoding']='pose_ref indexes obstacle_pose_frames; all transforms remain in native world'
    return packed


def unpack_geometry(packed):
    geometry=deepcopy(packed)
    frames=geometry.pop('obstacle_pose_frames',{})
    geometry.pop('obstacle_pose_encoding',None)
    for obstacle in geometry.get('obstacles') or []:
        if 'pose_ref' in obstacle:
            obstacle['pose']=frames[obstacle.pop('pose_ref')]
    return geometry


def recovery_geometry(geometry):
    """Small current-scene facts after a rejected plan; no grids or candidates.

    This is decision context, not another plan or permission. Keep the complete
    geometry query available when the next strategy needs more detail.
    """
    if not isinstance(geometry,dict) or geometry.get('schema')!='f1-geometric-tools-v1':
        raise ValueError('recovery_geometry_contract_invalid')
    def bounded(value,depth=0):
        if depth>5:return None
        if value is None or isinstance(value,bool):return value
        if isinstance(value,str):return value[:200]
        if isinstance(value,(int,float)):return value if math.isfinite(value) else None
        if isinstance(value,(list,tuple)):return [bounded(v,depth+1) for v in value[:16]]
        if isinstance(value,dict):
            return {str(k)[:64]:bounded(v,depth+1) for k,v in list(value.items())[:24]
                    if k not in ('local_collision_evidence','local_collision_evidence_ref','free_space_views','min_depth_m',
                                 'fine_depth_zlib_base64','grasp_suggestions')}
        return None
    result=dict(schema='f1-recovery-geometry-v1',motion_authorized=False,
                detail_tool='robo.geometry()',omitted_fields=[])
    # Ordering preserves source, current pose and support before optional detail.
    for key in ('observation_id','calibration_id','measurement_received_ns','world_frame',
                'geometry_status','geometry_scope','current_tcp_pose','object_pose','hole_pose',
                'object_held','entities','support','available_skills','cartesian_motion',
                'missing_entities','local_environment','object_local_box','tool_geometry',
                'fixture_transit_available','aperture_required_for','quality_warnings'):
        if key not in geometry:continue
        item=bounded(geometry[key]);candidate=dict(result,**{key:item})
        if len(json.dumps(candidate,ensure_ascii=False).encode())<=5500:result[key]=item
        else:result['omitted_fields'].append(key)
    return result

"""Object-relative search hints, not a pose whitelist or execution permission."""
from dataclasses import replace
import math

import numpy as np

from .contracts import HarnessError
from .geometry import transform
from .grasp_candidates import apply_descriptor
from .candidate_ranking import rank_grasp_choices
from .placement_advisory import placement_advisory
from .skills import propose, grasp_route_lookahead


def square_seeds(scene):
    """Sample face directions and size-scaled contact offsets in object axes.

    These are search seeds, not a claim that a finger can establish contact.
    The nominal calibration is retained as a seed; continuous model proposals
    remain accepted by the existing preview API.
    """
    nominal=transform(scene.object_grasp_eef)@transform(scene.eef_tip_reference)
    box=scene.object_local_box
    if box is None:return []
    width=float(min(np.asarray(box['max'])[:2]-np.asarray(box['min'])[:2]))
    if not math.isfinite(width) or width<=0:return []
    seeds=[dict(strategy='object_tcp',grasp_tcp_pose=nominal.ravel().tolist(),
                approach_direction_object=None,approach_distance_m=None)]
    for yaw in (0,90,180,270):
        z=math.radians(yaw);cz,sz=math.cos(z),math.sin(z)
        rz=np.array([[cz,-sz,0],[sz,cz,0],[0,0,1.]])
        for pitch in (0,30,45,60):
            x=math.radians(pitch);cx,sx=math.cos(x),math.sin(x)
            rotation=rz@np.array([[1.,0,0],[0,cx,-sx],[0,sx,cx]])
            direction=rotation[:,1].copy();direction[2]=0
            direction/=np.linalg.norm(direction)
            for offset in (0.,.5,1.):
                pose=np.eye(4);pose[:3,:3]=rotation
                pose[:3,3]=nominal[:3,3]+offset*width*direction
                seeds.append(dict(strategy='object_tcp',grasp_tcp_pose=pose.ravel().tolist(),
                    approach_direction_object=direction.tolist(),approach_distance_m=2*width))
    return seeds


def suggest_grasps(scene,*,placement_scene=None,prior=None,camera=None,limit=6):
    """Rank a bounded geometric sample without IK or robot-side effects.

    Blocked seeds remain suggestions after clear ones; an empty/blocked sample
    never declares the task unreachable. Full native validation is mandatory.
    """
    result=dict(source='object_face_geometry',candidates=[],sampled_count=0,
        numerical_feasibility_checked=False,motion_authorized=False,
        exhaustive=False,continuous_proposals_allowed=True)
    if not scene.parameterized_grasp or scene.object_held is True:
        return dict(result,status='not_applicable')
    seeds=square_seeds(scene);result['sampled_count']=len(seeds)
    _,scores=rank_grasp_choices(scene,[(d,0) for d in seeds],prior,camera=camera)
    rows=[]
    for score in scores:
        descriptor=seeds[score['index']]
        row=dict(parameters={k:v for k,v in descriptor.items() if k!='strategy'},
                 geometry_status='unavailable',score=score['score'])
        try:
            selected=replace(apply_descriptor(scene,descriptor),grasp_candidate_variant=0)
            approach=propose('approach_grasp',selected)
            row['geometry_status']='approach_clear'
            if placement_scene is not None:
                future=grasp_route_lookahead(selected,approach,placement_scene=placement_scene)
                row['geometry_status']=placement_advisory([dict(grasp_route_lookahead=future)])['status']
        except HarnessError as exc:
            # A finite geometric sample cannot establish global infeasibility.
            row.update(geometry_status='predicted_blocked',reason=str(exc).split(':',1)[0])
        rows.append(row)
    priority={'predicted_clear':0,'approach_clear':1,'unavailable':2,'predicted_blocked':3}
    rows.sort(key=lambda r:(priority[r['geometry_status']],r['score'] is None,r['score'] or 0.))
    # Try distinct orientations before several offsets of the same orientation.
    # A near-singular family otherwise consumes repeated native solves despite
    # every endpoint being geometrically clear. This only changes advice order.
    counts={};diverse=[]
    for row in rows:
        rotation=transform(row['parameters']['grasp_tcp_pose'])[:3,:3]
        key=(priority[row['geometry_status']],tuple(rotation.ravel()))
        occurrence=counts.get(key,0);counts[key]=occurrence+1
        diverse.append((priority[row['geometry_status']],occurrence,row))
    diverse.sort(key=lambda item:(item[0],item[1]))
    rows=[item[2] for item in diverse]
    result.update(status='advice_only',candidates=rows[:limit],
        geometrically_clear_count=sum(r['geometry_status'] in ('predicted_clear','approach_clear') for r in rows))
    return result

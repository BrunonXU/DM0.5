"""Empty-tool departure above, right of, then away from the measured bucket."""
from itertools import product
import numpy as np
from .contracts import HarnessError
from .geometry import transform, tool_sweep_clear

# Finite, reviewed Cartesian recipes, not absolute recorded joint commands.
# (fraction toward ready, upward bend in metres, outward bend in metres)
# All keep departure/right-clearance and use the same complete scene checks.
RETURN_BENDS = {1: (.25, .02, 0.), 2: (.25, .08, 0.), 3: (.25, .02, .08)}


def validate_return_rotation_fraction(value):
    if type(value) not in (int,float) or value not in (0.,.33,.5):
        raise HarnessError('unregistered_return_rotation_fraction')
    return float(value)


def corners(box):
    low,high=np.asarray(box['min'],float),np.asarray(box['max'],float)
    if low.shape!=(3,) or high.shape!=(3,) or not np.isfinite([low,high]).all() or np.any(low>=high):
        raise HarnessError('invalid_return_route_box')
    points=np.array(list(product(*zip(low,high))))
    pose=transform(box.get('pose',np.eye(4)))
    return points@pose[:3,:3].T+pose[:3,3]


def propose_return(*,current,home,hole,obstacles,tool_boxes,tip,radius_m,
                   right_axis,clearance_m,tool_margin_m=0.,variant=0,continuous_transit=False,
                   rotation_fraction=0.):
    """A geometric candidate, not permission to move or proof of an empty hand.

    right_axis is bound to the robot's world convention, never the image.
    Obstacles must already follow the latest measured fixture pose.
    The normal owner, IK, joint limits and actual FK sweep remain mandatory.
    """
    if type(variant) is not int or variant not in (0,*RETURN_BENDS):
        raise HarnessError('unregistered_candidate_variant')
    if type(continuous_transit) is not bool:raise HarnessError('invalid_continuous_return_transit')
    rotation_fraction=validate_return_rotation_fraction(rotation_fraction)
    def stage(name,pose):
        value=dict(name=name,goals=[pose.tolist()],gripper=None,eef_object=None)
        if continuous_transit and name in ('right_of_bucket','return_bend'):
            value['continuous_return_transit']=True
        return value
    current,home,hole=map(transform,(current,home,hole))
    right=np.asarray(right_axis,float)
    if (right.shape!=(3,) or not np.isfinite(right).all() or not np.isclose(np.linalg.norm(right),1.)
            or not np.isfinite(clearance_m) or clearance_m<=0
            or not np.isfinite(tool_margin_m) or tool_margin_m<0 or not tool_boxes):
        raise HarnessError('registered_return_route_parameters_required')
    up=hole[:3,2]
    # Allow a tilted lid, but require that robot-right has a usable lateral component.
    lateral=right-up*np.dot(right,up)
    if np.linalg.norm(lateral)<.5:raise HarnessError('return_right_axis_not_lateral')
    lateral/=np.linalg.norm(lateral)
    bucket=[o for o in obstacles if o.get('role')=='bucket' and o.get('type')!='halfspace']
    if not bucket:raise HarnessError('measured_bucket_geometry_required_for_return')
    bucket_points=np.concatenate([corners(o) for o in bucket])
    local_tool=np.concatenate([corners(b) for b in tool_boxes])
    margin=clearance_m+tool_margin_m
    def points(pose):return local_tool@pose[:3,:3].T+pose[:3,3]
    top=float(np.max(bucket_points@up))
    edge=float(np.max(bucket_points@lateral))
    lift=current.copy()
    lift[:3,3]+=up*max(0.,top+margin-float(np.min(points(lift)@up)))
    side=lift.copy()
    side[:3,3]+=lateral*max(0.,edge+margin-float(np.min(points(side)@lateral)))
    if rotation_fraction:
        from scipy.spatial.transform import Rotation, Slerp
        # Departure retains the insertion orientation. Only the subsequent
        # lateral segment may turn; its full swept tool still must clear the
        # bucket. Rotation can enlarge lateral extent, so extend outward as
        # needed without reducing the original right-side displacement.
        side[:3,:3]=Slerp([0.,1.],Rotation.from_matrix([lift[:3,:3],home[:3,:3]]))(
            rotation_fraction).as_matrix()
        side[:3,3]+=lateral*max(0.,edge+margin-float(np.min(points(side)@lateral)))
    # Travel above the lid to the ready location before descending. Home
    # rotation is not allowed while the tool is still engaged in the aperture.
    high=home.copy()
    high[:3,3]+=up*max(0.,top+margin-float(np.min(points(high)@up)))
    prefix=[('leave_hole',lift),('right_of_bucket',side)]
    before=current
    for name,pose in prefix:
        if not tool_sweep_clear(before,pose,tip,radius_m,obstacles,
                                margin_m=tool_margin_m,local_boxes=tool_boxes):
            raise HarnessError('return_route_sweep_blocked:'+name)
        before=pose
    if variant:
        from scipy.spatial.transform import Rotation, Slerp
        fraction,height,outward=RETURN_BENDS[variant]
        bend=side.copy()
        bend[:3,3]=(1-fraction)*side[:3,3]+fraction*home[:3,3]+height*up+outward*lateral
        bend[:3,:3]=Slerp([0.,1.],Rotation.from_matrix([side[:3,:3],home[:3,:3]]))(.5).as_matrix()
        tail=[('return_bend',bend),('ready',home)]
        before=side
        for name,pose in tail:
            if not tool_sweep_clear(before,pose,tip,radius_m,obstacles,
                                    margin_m=tool_margin_m,local_boxes=tool_boxes):
                raise HarnessError('return_route_sweep_blocked:'+name)
            before=pose
        return dict(kind='motion',skill='withdraw_reset',
            stages=[stage(name,pose) for name,pose in prefix+tail],
            evidence=dict(bucket_top=top,bucket_right_edge=edge,right_axis=lateral.tolist(),
                clearance_m=clearance_m,placement_success_separate=True,variant='registered_bend_'+str(variant),
                geometric_candidates=1,recipe=dict(fraction=fraction,height_m=height,outward_m=outward,rotation_fraction=.5)))
    # Do not impose a high detour when the original sweep proves a shorter
    # right-side route clear. Every candidate still begins with hole departure.
    lower=home.copy()
    lower[:3,3]=side[:3,3]+up*np.dot(home[:3,3]-side[:3,3],up)
    candidates=[('direct_from_right',[('ready',home)]),
                ('lower_outside_bucket',[('lower_outside_bucket',lower),('ready',home)]),
                ('above_ready',[('above_ready',high),('ready',home)])]
    feasible=[]
    for variant,tail in candidates:
        before=side;distance=0.;clear=True
        for name,pose in tail:
            if not tool_sweep_clear(before,pose,tip,radius_m,obstacles,
                                    margin_m=tool_margin_m,local_boxes=tool_boxes):
                clear=False;break
            distance+=float(np.linalg.norm(pose[:3,3]-before[:3,3]));before=pose
        if clear:feasible.append((distance,variant,tail))
    if not feasible:raise HarnessError('return_route_sweep_blocked:all_ready_routes')
    _,variant,tail=min(feasible,key=lambda item:item[0])
    result=[stage(name,pose) for name,pose in prefix+tail]
    return dict(kind='motion',skill='withdraw_reset',stages=result,
                evidence=dict(bucket_top=top,bucket_right_edge=edge,right_axis=lateral.tolist(),
                              clearance_m=clearance_m,placement_success_separate=True,variant=variant,
                              geometric_candidates=len(feasible)))

"""Timed references for the existing rate/acceleration/jerk-limited F1 owner.

These are references, not publications or measured completion certificates.
Gripper changes and semantic stage ends remain explicit barriers.
"""
import numpy as np


def reference_speed(fixture, skill):
    """Select a reviewed reference pace; actuator limits remain owner-owned.

    Overrides apply to approach, loaded transfer, fine insertion, uncertain-grasp
    observation and confirmed-empty return. Grasp and gripper
    barriers keep the base pace. Approach/observation can slow independently when
    output smoothing otherwise leaves its checked departure path.
    This is not a change to the original controller velocity/acceleration/jerk.
    """
    base=fixture.get('reference_velocity_deg_s')
    overrides=fixture.get('reference_velocity_by_skill_deg_s',{})
    if fixture.get('execution_mode')!='timed_reference':
        raise ValueError('phase_reference_speed_requires_timed_reference')
    if not isinstance(overrides,dict) or set(overrides)-{'approach_grasp','transfer_above_hole','insert','recover_grasp','withdraw_reset'}:
        raise ValueError('unregistered_phase_reference_speed')
    for speed in [base,*overrides.values()]:
        if type(speed) not in (int,float) or not np.isfinite(speed) or speed<=0:
            raise ValueError('invalid_reference_velocity')
    return float(overrides.get(skill,base))


def stage_requires_stop(stage):
    """Only explicitly registered empty-return transit points may stream through.

    Departure, final ready and every gripper boundary remain barriers. The
    revalidator binds this annotation back to the trusted return recipe.
    """
    continuous=stage.get('continuous_return_transit',False)
    if type(continuous) is not bool:
        raise ValueError('invalid_continuous_return_transit')
    if continuous and stage.get('semantic_stage') not in ('right_of_bucket','return_bend'):
        raise ValueError('unregistered_continuous_return_stage')
    return not continuous


def preclosure_checkpoint(plan,index,measured,anchor):
    """Owner-only proof that no closing target has been reached/published.

    An already closed or ambiguous grasp cannot replay its closure/lift stages.
    The caller separately requires a confirmed empty pre-motion observation.
    """
    if anchor is None or not 0<=index<len(plan.targets):return False
    opening=anchor[15]
    changes=[i for i,t in enumerate(plan.targets) if t[15]!=opening]
    if not changes or index>=changes[0]:return False
    first=plan.targets[changes[0]][15]
    return bool(first>opening and np.isfinite(measured[15])
                and abs(measured[15]-opening)<=plan.gripper_tolerance)


def schedule(targets, anchor, geometry_trace, velocity_deg_s):
    q=np.asarray(targets,float);anchor=np.asarray(anchor,float)
    if (q.ndim!=2 or q.shape[1]!=16 or not 1<=len(q)<=100 or anchor.shape!=(16,)
            or not np.isfinite(q).all() or not np.isfinite(anchor).all()
            or not np.isfinite(velocity_deg_s) or velocity_deg_s<=0):
        raise ValueError('invalid_trajectory_reference')
    # A 20ms minimum is the existing owner target clock, not an arrival wait.
    intervals=np.maximum(np.max(np.abs(np.diff(np.vstack([anchor,q]),axis=0)[:,7:14]),axis=1)
                         /velocity_deg_s,.02)
    stops={len(q)-1}
    for stage in geometry_trace:
        stop=stage['end_index']-1
        if not 0<=stop<len(q):raise ValueError('invalid_trajectory_stage')
        if stage_requires_stop(stage):stops.add(stop)
    previous=anchor
    for i,row in enumerate(q):
        if row[15]!=previous[15]:
            if i:stops.add(i-1)
            stops.add(i)
        previous=row
    return dict(schema='f1-timed-reference-v1',anchor=anchor.tolist(),
                times_s=np.cumsum(intervals).tolist(),stops=sorted(stops),
                reference_velocity_deg_s=float(velocity_deg_s))


def validate_schedule(value,targets):
    if not isinstance(value,dict) or value.get('schema')!='f1-timed-reference-v1':return False
    try:
        q=np.asarray(targets,float);a=np.asarray(value['anchor'],float);t=np.asarray(value['times_s'],float)
        stops=value['stops'];speed=float(value['reference_velocity_deg_s'])
        if (q.ndim!=2 or q.shape[1]!=16 or not 1<=len(q)<=100 or not np.isfinite(q).all()
                or a.shape!=(16,) or t.shape!=(len(q),) or not np.isfinite(a).all()
                or not np.isfinite(t).all() or not np.isfinite(speed) or speed<=0
                or np.any(np.diff(np.r_[0.,t])<=0) or not stops or stops[-1]!=len(q)-1
                or any(type(i) is not int or not 0<=i<len(q) for i in stops)
                or stops!=sorted(set(stops))):return False
        velocities=np.abs(np.diff(np.vstack([a,q]),axis=0)[:,7:14])/np.diff(np.r_[0.,t])[:,None]
        if np.any(velocities>speed+1e-7):return False
        for i,row in enumerate(q):
            prev=a if i==0 else q[i-1]
            if row[15]!=prev[15] and (i not in stops or i and i-1 not in stops):return False
        return True
    except (KeyError,TypeError,ValueError,IndexError):return False


def sample(value,targets,*,start_index,elapsed_s):
    """Sample one segment; never run past a gripper or semantic boundary."""
    q=np.asarray(targets,float);t=np.asarray(value['times_s'],float)
    end=next(i for i in value['stops'] if i>=start_index)
    origin=0. if start_index==0 else t[start_index-1]
    stamp=min(origin+max(0.,elapsed_s),t[end])
    index=max(start_index,min(int(np.searchsorted(t,stamp,side='left')),end))
    before=np.asarray(value['anchor']) if index==0 else q[index-1]
    previous=0. if index==0 else t[index-1]
    alpha=float(np.clip((stamp-previous)/(t[index]-previous),0,1))
    target=before.copy();target[7:14]=(1-alpha)*before[7:14]+alpha*q[index,7:14]
    # Fingers never interpolate across a movement segment or close early.
    target[15]=q[index,15] if stamp>=t[index] else before[15]
    return target,index,stamp>=t[end],end

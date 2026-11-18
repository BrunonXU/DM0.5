"""Bounded offline preview using the original F1 owner and memory transport.

No sockets, ROS publishers or hardware interfaces are constructed here. This
component predicts a declared reference timeline from a reset/rest state and
ideal following. It does not infer a live owner's hidden filter/braking state,
predict future perception, authorize execution or certify physical motion.
"""
import math
import time
from types import SimpleNamespace

import numpy as np


class PreviewBudgetExceeded(RuntimeError):
    pass


class MemoryTransport:
    rmw_gids={k:'offline-preview' for k in ('joint_command','left_gripper_command','right_gripper_command')}
    own_gids={}

    def prepare(self,key):pass
    def assert_exclusive_cached(self,key):pass
    def publish(self,key,value):pass
    def release(self,key):pass


def validate_input(value,max_samples):
    if value.get('schema')!='f1-reference-preview-input-v1' or value.get('initial_state_kind')!='reset_at_rest':
        raise ValueError('preview_requires_explicit_rest_initialization')
    for name,size in (('full22',22),('initial_state16',16)):
        a=np.asarray(value[name],float)
        if a.shape!=(size,) or not np.isfinite(a).all():raise ValueError('invalid_preview_'+name)
    mask=value['state_valid_mask']
    if not isinstance(mask,list) or len(mask)!=16 or any(type(x) is not bool for x in mask):
        raise ValueError('invalid_preview_state_mask')
    records=value['references'];stamp=value['initialized_at_ns']
    if type(stamp) is not int or stamp<0:raise ValueError('invalid_preview_initial_time')
    if type(max_samples) is not int or not 1<=max_samples<=5000:
        raise ValueError('invalid_preview_sample_budget')
    if not isinstance(records,list) or not 1<=len(records)<=max_samples:
        raise ValueError('preview_sample_budget_exceeded')
    for row in records:
        now=row['virtual_ns'];q=np.asarray(row['reference16'],float)
        if type(now) is not int or not 0<now-stamp<=50_000_000:
            raise ValueError('preview_reference_clock_invalid')
        if q.shape!=(16,) or not np.isfinite(q).all():raise ValueError('invalid_preview_reference')
        if type(row['new_motion_reference']) is not bool or type(row.get('hold',False)) is not bool:
            raise ValueError('invalid_preview_reference_flags')
        if row.get('request_id') is not None and not isinstance(row['request_id'],str):
            raise ValueError('invalid_preview_request_id')
        stamp=now


def _make_owner(config,mapping,gate,source):
    from f1dagger.robot_io import F1Owner,make_output
    owner=F1Owner(MemoryTransport(),source,mapping,make_output(config),gate,powered=True)
    owner.preventive_boundary_recovery=True
    return owner


def preview_references(value,*,config,mapping,gate,max_samples=5000,max_wall_s=15.,clock=time.monotonic):
    """No future submitted/measured output is accepted as prediction input.

    Timestamps are retained (not rebased), since floating-point elapsed time can
    affect the original stateful smoother. Disabled ticks hold this preview's
    own last output, rather than copying a future recorded hold position.
    """
    validate_input(value,max_samples)
    if type(max_wall_s) not in (int,float) or not math.isfinite(max_wall_s) or max_wall_s<=0:
        raise ValueError('invalid_preview_wall_budget')
    full=np.array(value['full22'],float);measured=np.array(value['initial_state16'],float)
    stamp=[value['initialized_at_ns']]
    source=SimpleNamespace(feedback=lambda:(full.copy(),measured.copy(),(stamp[0],)*16),
        state_valid_mask=tuple(value['state_valid_mask']))
    owner=_make_owner(config,mapping,gate,source);rows=[];started=clock()
    try:
        owner.initialize(stamp[0]/1e9);owner.prepare()
        for reference in value['references']:
            if clock()-started>max_wall_s:raise PreviewBudgetExceeded('output_preview_wall_budget_exceeded')
            stamp[0]=reference['virtual_ns'];now=stamp[0]/1e9
            full[7:14]=measured[7:14]
            if reference.get('hold',False):owner.hold(now)
            active=reference['new_motion_reference']
            command=reference['reference16'] if active else owner.last_submitted.copy()
            owner.approve(command,now,enabled=(False,active))
            output,_,evidence=owner.publish(now)
            if not evidence['publication_complete']:raise RuntimeError('preview_publication_incomplete')
            rows.append(dict(virtual_ns=stamp[0],request_id=reference.get('request_id'),
                reference16=owner.selected.tolist(),submitted16=output.tolist(),measured16=measured.tolist(),
                new_motion_reference=active,simulation=True,output_evidence=evidence))
            measured[:]=output
        if clock()-started>max_wall_s:raise PreviewBudgetExceeded('output_preview_wall_budget_exceeded')
        return dict(schema='f1-reference-output-preview-v1',rows=rows,compute_s=clock()-started,
            physical_motion=False,robot_connected=False,execution_authorized=False,
            limitations=['Reset/rest initial state, declared reference timing and ideal actuator following.',
                'Does not reconstruct a live moving controller, its braking certificate or future stage timing.',
                'Owner native limits/collisions are checked; task-scene output sweeps must be checked separately.'])
    finally:
        owner.close()

import json

import numpy as np
import pytest

from f1_harness.hybrid import PlacementOpportunity, PlacementRegion, checked_review, review_once


def monitor():
    return PlacementOpportunity(PlacementRegion(tuple(np.eye(4).ravel()), .2, .3, -.05), open_range=(0, 20))


def test_spatial_entry_once_per_grasp_and_late_release_discards():
    m = monitor(); pose = np.eye(4); pose[2, 3] = .15
    outside = pose.copy(); outside[0, 3] = .8
    scope = ('owner', 'session', 1)
    assert not m.update(gripper=100, pose=outside, scope=scope)['eligible']
    current = m.update(gripper=100, pose=pose, scope=scope)
    identity = m.claim(current)
    assert not m.update(gripper=100, pose=pose, scope=scope)['eligible']
    assert not m.applicable(identity, m.update(gripper=0, pose=pose, scope=scope))
    next_grasp = m.update(gripper=100, pose=pose, scope=scope)
    assert next_grasp['eligible'] and next_grasp['identity'] != identity
    assert not m.applicable(identity, next_grasp)


def test_authority_change_and_exit_invalidate_even_when_gripper_closed():
    m = monitor(); pose = np.eye(4)
    current = m.update(gripper=100, pose=pose, scope=('owner', 'session', 1))
    identity = m.claim(current)
    assert not m.applicable(identity, m.update(gripper=100, pose=pose, scope=('owner', 'session', 2)))
    pose[0, 3] = .4
    assert not m.applicable(identity, m.update(gripper=100, pose=pose, scope=('owner', 'session', 1)))


def test_review_is_one_generation_no_tool_loop_or_motion_interface():
    calls=[]
    decision=dict(decision='REFINE', reason='当前偏向孔口边缘', alignment_quarter_turns=1, retreat_first=True,head_entities=[],right_entities=[])
    class Planner:
        def request(self, **kw):
            calls.append(kw)
            return {'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(decision)}]}]}
    assert review_once(Planner(), {'phase':'placement'}, {'head':b'png'}) == decision
    assert len(calls)==1 and calls[0]['tools']==[]
    assert calls[0]['text']['format']['strict'] is True


@pytest.mark.parametrize('patch', [dict(alignment_quarter_turns=True), dict(alignment_quarter_turns=4),
    dict(retreat_first=1), dict(decision='STOP'), dict(joints=[0]*7)])
def test_reviewer_cannot_expand_action_surface(patch):
    value=dict(decision='KEEP',reason='继续',alignment_quarter_turns=0,retreat_first=False,head_entities=[],right_entities=[])
    value.update(patch)
    with pytest.raises(ValueError):checked_review(value)

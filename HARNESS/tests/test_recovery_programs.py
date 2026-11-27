from copy import deepcopy
import pytest
from f1_harness.contracts import HarnessError
from f1_harness.recovery_programs import registered_program,validate_program,matches,compile_library


class Robot:
    def __init__(self,initial,following):self.initial=initial;self.following=list(following);self.calls=[]
    def skill(self,name):self.calls.append(('skill',name));return self.initial.copy()
    def recheck(self,name,request):self.calls.append(('recheck',name,request));return self.following.pop(0)
    def resume(self,name,request):self.calls.append(('resume',name,request));return self.following.pop(0)


@pytest.mark.parametrize('budget',[0,1,2])
@pytest.mark.parametrize('resolved', [False, True])
def test_program_bounds_verification_only_calls_and_never_replays_completed_motion(budget, resolved):
    pending=dict(outcome='pending_review',verification_request_id='completed-motion')
    last = dict(outcome='success') if resolved else pending
    robot=Robot(pending,[pending.copy() for _ in range(5)]+[last])
    ns={'robo':robot};exec(compile_library(budget),ns)
    assert ns['skill']('approach_grasp')==last
    assert robot.calls==[('skill','approach_grasp')]+[('recheck','approach_grasp','completed-motion')]*6


def test_readonly_preparation_failure_returns_latest_reason_with_original_request():
    pending=dict(outcome='pending_review',verification_request_id='transport-1')
    refreshed=dict(pending,continuation_submitted=False,observation_retry_reason='anchor_identity_changed')
    robot=Robot(pending,[refreshed])
    ns={'robo':robot};exec(compile_library(2),ns)
    assert ns['evidence']('transfer_above_hole',pending)==refreshed
    assert robot.calls==[('recheck','transfer_above_hole','transport-1')]


@pytest.mark.parametrize('broken',['wrong_reason','unconfirmed_hold','unknown_request','cancelled',None])
def test_resume_requires_known_progress_and_does_not_retry_other_failures(broken):
    result=dict(outcome='failure',reason='motion_stalled',cancel_stage='measured_stationary',resume_request_id='move')
    if broken=='wrong_reason':result['reason']='unknown_publication'
    if broken=='unconfirmed_hold':result['cancel_stage']='owner_hold_committed'
    if broken=='unknown_request':result.pop('resume_request_id')
    if broken=='cancelled':result['outcome']='interrupted'
    program=registered_program('resume_known_progress')
    assert matches(program,'grasp',result) is (broken is None)
    robot=Robot(result,[{'outcome':'success'}]);ns={'robo':robot};exec(compile_library(2),ns)
    ns['motion']('grasp')
    assert robot.calls==[('skill','grasp')]+([('resume','grasp','move')] if broken is None else [])


def test_notes_and_modified_program_cannot_be_compiled_as_new_actions():
    value=registered_program('verify_completed_motion');validate_program(value)
    changed=deepcopy(value);changed['operation']='direct_joint_command'
    with pytest.raises(HarnessError):validate_program(changed)
    with pytest.raises(HarnessError):validate_program({'id':'invented','code':'danger()'})
    for budget in (True,-1,5,1.5):
        with pytest.raises(HarnessError):compile_library(budget)
    for budget in (True,-1,4,1.5):
        with pytest.raises(HarnessError):compile_library(0,budget)


@pytest.mark.parametrize('cancel_stage',['cancel_requested','measured_stationary'])
def test_loaded_loss_recovers_locally_only_after_measured_hold(cancel_stage):
    lost=dict(outcome='failure',reason='attachment_lost',cancel_stage=cancel_stage)
    calls=[]
    class Robot:
        def skill(self,name,**kwargs):
            calls.append(name)
            return lost if len(calls)==1 else dict(outcome='success')
    ns={'robo':Robot()};exec(compile_library(2,1),ns)
    result=ns['place_held_block']()
    if cancel_stage=='cancel_requested':
        assert calls==['transfer_above_hole'] and result['outcome']=='failure'
    else:
        assert calls==['transfer_above_hole','reobserve','regrasp','verify_grasp','transfer_above_hole',
                       'inspect_in_hand','align','insert','release','withdraw_reset','verify_placed']
        assert result['outcome']=='success'


def test_known_miss_reobserves_before_requesting_any_regrasp():
    missed=dict(outcome='failure',reason='grasp_not_attached',cancel_stage='measured_stationary')
    pending=dict(outcome='pending_review',reason='object_not_observed',no_motion=True)
    calls=[]
    class Robot:
        def skill(self,name,**kwargs):
            calls.append(name)
            return missed if name=='grasp' else pending
    ns={'robo':Robot()};exec(compile_library(1,1),ns)
    result=ns['grasp_and_place']()
    assert calls==['grasp','reobserve']
    assert result['program_stage']=='reobserve'
    assert result['prior_result']==missed
    assert result['recovery_hint']=='refresh_attachment_evidence'


@pytest.mark.parametrize('outcomes,expected_regrasps', [
    (['success'], 1),
    (['miss', 'success'], 2),
    (['miss', 'miss', 'miss'], 3),
    (['unknown'], 1),
])
def test_regrasp_budget_requires_a_current_miss_before_each_attempt(outcomes,expected_regrasps):
    miss = dict(outcome='failure',reason='grasp_not_attached',cancel_stage='measured_stationary')
    unknown = dict(outcome='pending_review',reason='grasp_held_evidence_missing')
    observations = iter(['miss', *outcomes])
    calls = []
    class Robot:
        def skill(self,name,**kwargs):
            calls.append(name)
            if name == 'verify_grasp':
                return {'miss':miss,'unknown':unknown,'success':dict(outcome='success')}[next(observations)]
            return dict(outcome='success')
    ns={'robo':Robot()};exec(compile_library(4,3),ns)
    result=ns['grasp_and_place'](start='verify_grasp')
    assert calls[:1+3*expected_regrasps] == ['verify_grasp']+['reobserve','regrasp','verify_grasp']*expected_regrasps
    assert calls.count('regrasp') == expected_regrasps
    assert result['outcome'] == {'miss':'failure','unknown':'pending_review','success':'success'}[outcomes[-1]]


def test_grasp_and_verify_share_the_same_three_regrasp_attempts():
    miss = dict(outcome='failure',reason='grasp_not_attached',cancel_stage='measured_stationary')
    calls = []
    class Robot:
        def skill(self,name,**kwargs):
            calls.append(name)
            return miss if name in ('grasp','verify_grasp') else dict(outcome='success')
    ns={'robo':Robot()};exec(compile_library(4,3),ns)
    assert ns['grasp_and_place']() == dict(miss,program_stage='verify_grasp')
    assert calls == ['grasp','reobserve','regrasp','verify_grasp']+['reobserve','regrasp','verify_grasp']*2

"""Variable dual-arm prefix, early reobserve, and bounded IK pause/resume."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from test_inference_execution import NOW,BINDING,response,capture,snapshot,profile
from test_inference_runtime import loop
from f1_policy_client.execution import prepare_prefix,PREFIX_POLICY,IKPrefixRejected
from f1_policy_client.native import activate_with_ik_pause,wait_ik_resume
from f1_policy_client import runtime
from f1_replay.runtime import ReplayError,ReplayInterrupted

OPTIONS={'position_tolerance_mm':5.,'rotation_tolerance_deg':.05}
RECIPE='f1_ikfk_demo_pos5mm_rot005deg_v1'

def configure(p):
    p.update(inference_prefix_policy=PREFIX_POLICY,inference_ik_recipe_version=RECIPE)

def adapted(r,count):
    r=deepcopy(r);r.update(prefix_policy=PREFIX_POLICY,ik_recipe_version=RECIPE,ik_options=OPTIONS,
                          valid_steps=count,truncation=None if count==20 else {'reason':'ik_rejected','step':count,'arm':1})
    if count:
        for key in ('joint_targets_deg','gripper_targets','base_velocity_targets'):r[key]=r[key][:count]
    else:
        r.update(status='rejected',reason='ik_rejected',ik={'step':0,'arm':1})
        for key in ('joint_targets_deg','gripper_targets','base_velocity_targets'):r.pop(key,None)
    return r

def prepare(r,p):
    return prepare_prefix(r,capture(),snapshot(),p,now_ns=NOW,request_id='request-7',expected_model_binding=BINDING,max_observation_age_s=2)

@pytest.mark.parametrize('count',[1,3,15,19,20])
def test_exact_prefix_and_timing(count):
    p=profile();configure(p);r=adapted(response(),count);original=deepcopy(r)
    out=prepare(r,p)
    assert r==original and len(out.bundle.frames)==count
    assert out.bundle.duration_ns==round(count*1e9/30)
    for i,f in enumerate(out.bundle.frames):
        assert f.time_ns==round(i*1e9/30)
        assert list(f.action)==r['joint_targets_deg'][i]+r['gripper_targets'][i]+r['base_velocity_targets'][i]

@pytest.mark.parametrize('kind',['fixed_client','wrong_step','missing_capability','extra_point','wrong_binding','zero_ready','bool_count','hard_limit','wrist_limit'])
def test_invalid_partial_never_exposes_commands(kind):
    p=profile();configure(p);r=adapted(response(),3)
    if kind=='fixed_client':p.pop('inference_prefix_policy')
    elif kind=='wrong_step':r['truncation']['step']=4
    elif kind=='missing_capability':r.pop('prefix_policy')
    elif kind=='extra_point':r['joint_targets_deg'].append([0.]*14)
    elif kind=='wrong_binding':r['model_binding']={}
    elif kind=='zero_ready':r['valid_steps']=0
    elif kind=='bool_count':r['valid_steps']=True
    elif kind=='hard_limit':r['joint_targets_deg'][2][0]=999
    elif kind=='wrist_limit':r['joint_targets_deg'][2][5:7]=[60.,90.]
    with pytest.raises(ReplayError):prepare(r,p)

def test_zero_prefix_is_retryable_only_after_identity_checked():
    p=profile();configure(p);r=adapted(response(),0)
    with pytest.raises(IKPrefixRejected):prepare(r,p)
    r['model_binding']={}
    with pytest.raises(ReplayError) as error:prepare(r,p)
    assert not isinstance(error.value,IKPrefixRejected)

def test_only_operator_resume_restarts_exhausted_retry_burst():
    events=[]
    def attempt():
        events.append('request')
        if events.count('request')<=4:raise IKPrefixRejected({'ik':{'step':0}})
        return 'fresh-result'
    def pause(exc):
        assert events.count('request')==4;events.append('operator_resume')
    value=activate_with_ik_pause(attempt,3,lambda:True,lambda *x:events.append('retry'),pause)
    assert value=='fresh-result' and events[-2:]==['operator_resume','request']
    def cancel(exc):raise ReplayInterrupted('operator canceled')
    with pytest.raises(ReplayInterrupted):
        activate_with_ik_pause(lambda:(_ for _ in ()).throw(IKPrefixRejected({})),3,lambda:True,lambda *x:None,cancel)

def test_pause_polls_keeps_base_zero_and_can_abort(monkeypatch):
    events=[];result={'ik_pauses':0}
    backend=SimpleNamespace(stop_base=lambda:events.append('zero') or {},hold_arms=lambda:events.append('hold') or {})
    def wait(poll):
        assert 'hold' in events
        for _ in range(3):poll()
    wait_ik_resume(backend,lambda:events.append('tick'),lambda *x:None,result,powered=True,wait_for_enter=wait)
    assert result['ik_pauses']==1 and events.count('tick')>=5
    def cancel(poll):raise ReplayInterrupted('cancel')
    with pytest.raises(ReplayInterrupted):
        wait_ik_resume(backend,lambda:None,lambda *x:None,result,powered=True,wait_for_enter=cancel)

def test_end_to_end_partial_then_new_observation(loop,monkeypatch):
    doc,p,out,backends,policy,stops=loop;configure(p)
    doc['client'].update(inter_chunk_policy='sync_raw_h20_v1',ik_replan_retry_limit=3,ik_pause_on_exhausted=True)
    doc['client']['profile_overrides']={'inference_prefix_policy':PREFIX_POLICY}
    health=policy.health;infer=policy.infer;seen=[]
    monkeypatch.setattr(policy,'health',lambda self:dict(health(self),prefix_policy=PREFIX_POLICY))
    def responses(self,obs,request_id):
        seen.append((request_id,obs.captured_ns));return adapted(infer(self,obs,request_id),3 if len(seen)==1 else 20)
    monkeypatch.setattr(policy,'infer',responses)
    result=runtime.run(doc,p,out,powered=True,chunks=2)
    assert not result.get('error'),result
    assert result['partial_prefixes']==1 and result['completed_prefixes']==2
    assert result['native_points_published']==23 and len(backends[0].sent)==23
    assert seen[0][0]!=seen[1][0] and seen[0][1]<seen[1][1]
    assert result['ik_pauses']==0 and backends[0].enabled==1

def test_end_to_end_four_failures_pause_resume_same_run(loop,monkeypatch):
    doc,p,out,backends,policy,stops=loop;configure(p)
    doc['client'].update(inter_chunk_policy='sync_raw_h20_v1',ik_replan_retry_limit=3,ik_pause_on_exhausted=True)
    doc['client']['profile_overrides']={'inference_prefix_policy':PREFIX_POLICY}
    health=policy.health;infer=policy.infer;seen=[];enters=[]
    monkeypatch.setattr(policy,'health',lambda self:dict(health(self),prefix_policy=PREFIX_POLICY))
    def responses(self,obs,request_id):
        seen.append(request_id);return adapted(infer(self,obs,request_id),0 if len(seen)<=4 else 20)
    def enter(poll):
        enters.append(len(seen));poll()
    monkeypatch.setattr(policy,'infer',responses);monkeypatch.setattr(runtime,'wait_for_enter',enter)
    # Backend fixture represents the existing same-process hold API.
    cls=runtime.RosObservationBackend;monkeypatch.setattr(cls,'hold_arms',lambda self:{'held':True},raising=False)
    result=runtime.run(doc,p,out,powered=True,chunks=5)
    assert not result.get('error'),result
    assert enters==[0,4] and result['ik_pauses']==1 and result['ik_retries']==3
    assert len(set(seen))==5 and backends[0].enabled==1
    assert len(backends[0].sent)==20 and result['chunks_executed']==1

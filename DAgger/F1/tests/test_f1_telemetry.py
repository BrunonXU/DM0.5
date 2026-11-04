import json
import pytest
from f1dagger.telemetry import summarize_session


def test_summary_distinguishes_publication_from_motion_and_missing_evidence(tmp_path):
    j=tmp_path/'journal';j.mkdir()
    (j/'manifest.json').write_text(json.dumps({'complete': True}))
    def write(name,rows):
        (j/(name+'.jsonl')).write_text(''.join(json.dumps(r)+'\n' for r in rows))
    write('events',[
        dict(event='powered_policy_request', policy_epoch=0,request_id=1),
        dict(event='policy_transport_timing',policy_epoch=0,request_id=1),
        dict(event='powered_policy_request',policy_epoch=1,request_id=2),
        dict(event='session_termination', reason='signal',signal='SIGINT')])
    write('policy_chunks',[dict(policy_epoch=0,request_id=1,actions=[[0]*16]*20,observation_id=2)])
    write('control',[dict(monotonic_ns=10,policy_epoch=0,policy_request_id=1,source='policy',
        publication_complete=True,submitted=[2]*16,measured=[1]*16,
        policy_sampling=dict(lower_index=2,upper_index=3,alpha=.5)),
        dict(monotonic_ns=11,policy_epoch=0,policy_request_id=1,source='hold',
        publication_complete=False,submitted=None,measured=[1]*16)])
    result=summarize_session(tmp_path)
    assert result['missing_client_timing']==[[1,2]]
    assert result['published_control_rows']==1
    assert result['chunks'][0]['max_referenced_waypoint']==3
    assert result['chunks'][0]['control_rows']==1
    assert result['coverage']['controller_accepted_targets'] is False
    assert result['coverage']['physical_waypoint_arrival'] is False
    assert result['termination']['signal']=='SIGINT'
    assert result['max_submitted_feedback_error'][0]['signed_error']==1
    with pytest.raises(FileExistsError):summarize_session(tmp_path)


def test_rejected_request_keeps_timing_and_original_exception():
    from f1dagger.policy import F1PolicyEngine
    from f1dagger.policy_errors import IKRejected
    from types import SimpleNamespace
    events=[]
    rejection=IKRejected({'reason':'ik_rejected'})
    policy=SimpleNamespace(last_timing={})
    def infer(obs,rid,epoch):
        policy.last_timing=dict(request_id=rid,policy_epoch=epoch,outcome='ik_rejected',network_rtt_ms=200.)
        raise rejection
    policy.infer=infer
    engine=object.__new__(F1PolicyEngine)
    engine.policy=policy
    engine.emit=lambda event,**fields:events.append((event,fields))
    with pytest.raises(IKRejected) as error:engine._infer(SimpleNamespace(observation_id=7),2,3)
    assert error.value is rejection
    assert events[0][0]=='policy_transport_timing'
    assert events[0][1]['timing']['network_rtt_ms']==200.
    assert events[0][1]['error']=='IKRejected: ik_rejected'

"""Decision provenance regressions: actual prefixes, future labels and cancels."""
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from f1_toys.rlt_decision_replay import build_decision_transitions,prediction_key,_human_record
from test_rlt_local_replay import fixture


def decision_fixture():
    streams,result=fixture(assisted=True)
    plan=dict(phase='actor',model_id='model',model_version='version',policy_epoch=1,request_id=7,
              observation_id=0,observation_monotonic_ns=result['started_ns'],
              adopted_monotonic_ns=result['started_ns'])
    chunk=dict(plan,actions=[[0.]*16 for _ in range(16)])
    prediction=dict(plan,success=True,actions=copy.deepcopy(chunk['actions']),
                    rlt={'actor_version':'fixed'},prediction_replay=dict(
                        observation_anchor_state16=[0.]*16,
                        actor_normalized_actions32=[[i/100]*10+[0.]*22 for i in range(16)]))
    streams.update(policy_chunks=[chunk],events=[dict(event='rlt_plan_adopted',plan=plan,
                    monotonic_ns=plan['adopted_monotonic_ns'])])
    for i,c in enumerate(streams['control']):
        c['selected']=list(c['submitted'])
        if i<6:
            c['policy_request_id']=7
            u=i*.6
            lower=-1 if u<1 else int(u-1)
            upper=0 if u<1 else lower+1
            c['policy_sampling'].update(lower_index=lower,upper_index=upper,
                alpha=u if u<1 else u-1-lower,anchor=[0.]*16)
    return streams,result,{prediction_key(prediction):prediction}


def test_actor_uses_original_observation_and_only_consumed_prefix():
    streams,result,pred=decision_fixture()
    rows,audit=build_decision_transitions(streams,result,pred)
    r=rows[0]
    assert r['decision_kind']=='actor_request' and r['observation_id']==0
    assert r['request_id']==7 and sum(r['action_time_mask'])==3
    assert r['normalized_actions10']==[[i/100]*10 for i in range(3)]
    assert r['execution_fraction']==[1.]*3+[0.]*13
    assert r['execution_last_ns']<=r['endpoint_capture_ns']
    assert audit['retained_actor']==1
    assert sum(r['reward'] for r in rows)==1
    assert not rows[0]['terminated'] and rows[-1]['terminated']


def rtc_fixture():
    streams,result,predictions=decision_fixture()
    first=streams['events'][0]['plan']
    origin=result['started_ns']
    first['rtc']=dict(bootstrap=True,delay_steps=2,shift_steps=2,coverage_steps=16,
        plan_origin_ns=0,base_plan_id=0,committed_actions16=[],previous_absolute_eef10=[],rlt=True)
    second=dict(first,request_id=8,observation_id=3,
        observation_monotonic_ns=origin+60_000_000,adopted_monotonic_ns=origin+80_000_000,
        rtc=dict(first['rtc'],bootstrap=False,base_plan_id=7,plan_origin_ns=origin+round(1e9/30),
            shift_steps=1,delay_steps=1,committed_actions16=[[0.]*16],previous_absolute_eef10=[[0.]*10]*15))
    streams['events'].append(dict(event='rlt_plan_adopted',plan=second,monotonic_ns=second['adopted_monotonic_ns']))
    chunk=dict(streams['policy_chunks'][0],**{k:v for k,v in second.items() if k!='rtc'})
    streams['policy_chunks'].append(chunk)
    old=next(iter(predictions.values()))
    new=copy.deepcopy(old);new.update({k:second[k] for k in ('request_id','observation_id','observation_monotonic_ns')})
    predictions[prediction_key(new)]=new
    for plan,p in [(first,old),(second,new)]:
        p['prediction_replay'].update(rtc=plan['rtc'],prefix_contract='known_prefix_detached_v1')
    for c in streams['control'][:6]:
        second_owner=c['monotonic_ns']>=second['adopted_monotonic_ns']
        start=second['rtc']['plan_origin_ns'] if second_owner else origin
        c['policy_request_id']=8 if second_owner else 7
        u=(c['monotonic_ns']-start)*30/1e9
        lo=-1 if u<1 else int(u-1);hi=0 if u<1 else lo+1
        c['policy_sampling'].update(chunk_started_monotonic_ns=start,lower_index=lo,upper_index=hi,
                                    alpha=u if u<1 else u-1-lo,anchor=[0.]*16)
    return streams,result,predictions


def test_rtc_replay_proves_both_old_prefix_and_new_suffix_publications():
    streams,result,pred=rtc_fixture()
    rows,audit=build_decision_transitions(streams,result,pred)
    assert audit['retained_actor']==2, audit
    assert rows[0]['next_observation_id']==rows[1]['observation_id']==3
    assert rows[0]['execution_last_ns']<=rows[0]['endpoint_capture_ns']
    assert rows[1]['command_sequences'][0]==4  # Previous owner, after observation.
    assert rows[1]['execution_fraction'][0]==pytest.approx(.2)  # 0.8 was before this observation.
    assert rows[1]['request_id']==8 and sum(r['reward'] for r in rows)==1


def test_rtc_wrong_committed_target_is_not_admitted():
    streams,result,pred=rtc_fixture()
    streams['control'][3]['selected'][7]=1.
    _,audit=build_decision_transitions(streams,result,pred)
    assert audit['retained_actor']==0
    assert any('differs' in x['reason'] for x in audit['excluded'])


def test_rtc_off_grid_plan_cannot_be_rounded_into_execution_evidence():
    streams,result,pred=rtc_fixture()
    streams['control'][4]['policy_sampling']['chunk_started_monotonic_ns']+=1_000_000
    _,audit=build_decision_transitions(streams,result,pred)
    assert any('waypoint grid' in x['reason'] for x in audit['excluded'])


def test_unpublished_proposal_and_wrong_server_reply_do_not_become_actions():
    streams,result,pred=decision_fixture()
    other=dict(streams['policy_chunks'][0],request_id=8)
    streams['policy_chunks'].append(other)
    rows,_=build_decision_transitions(streams,result,pred)
    assert [r.get('request_id') for r in rows if r['decision_kind']=='actor_request']==[7]
    next(iter(pred.values()))['actions'][0][7]=3.
    rows,audit=build_decision_transitions(streams,result,pred)
    assert audit['retained_actor']==0 and audit['excluded'][0]['kind']=='actor'
    assert rows[-1]['reward']==1


def test_nonexecuted_selected_target_is_rejected():
    streams,result,pred=decision_fixture()
    streams['control'][2]['selected'][7]=.2
    _,audit=build_decision_transitions(streams,result,pred)
    assert audit['retained_actor']==0
    assert 'differs' in audit['excluded'][0]['reason']


def test_human_labels_are_future_targets_not_the_anchor_command():
    streams,result,pred=decision_fixture()
    for i,c in enumerate(streams['control'][6:]):c['selected'][7]=i
    rows,_=build_decision_transitions(streams,result,pred)
    r=rows[-1]
    assert all(t>r['decision_ns'] for t in r['action_grid_ns'])
    assert r['selected16'][0][7]>0
    assert r['action_grid_ns'][-1]==r['next_decision_ns']


def test_human_anchor_advances_past_compute_wait_without_losing_terminal():
    streams,result=fixture(assisted=True)
    for c in streams['control']:c['selected']=list(c['submitted'])
    streams['control'][6]['source']='human_compute_wait'
    row=dict(observation_id=6,next_observation_id=12,decision_ns=streams['control'][7]['monotonic_ns'],
             terminated=True,reward=1.,truncated=False,action_control_sequences=[6])
    for i,c in enumerate(streams['control']):c['stream_sequence']=i
    r=_human_record(row,{o['observation_id']:o for o in streams['observations']},
                   streams['control'],[c['monotonic_ns'] for c in streams['control']],gamma=.999,fps=30)
    assert r['observation_id']==7 and r['reward']==1 and r['terminated']
    assert r['execution_fraction'][sum(r['action_time_mask']):]==[0.]*(16-sum(r['action_time_mask']))
    assert r['original_action_control_sequences']==[6]
    assert r['action_control_sequences'][0]>7

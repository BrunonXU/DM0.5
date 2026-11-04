"""Independent continuous episodes using real journals and synthetic inputs."""
from dataclasses import replace
from concurrent.futures import Future
from types import SimpleNamespace
import json
import time

import numpy as np
import pytest
from dagger_core.hil_supervisor import HILState
from f1dagger.demonstration import DemonstrationRecorder
from f1dagger.policy import F1PolicyEngine
from test_f1_policy import Source, observation
from test_real_observation_shadow import packet
from test_f1_demonstration import wait_for


def make_episode_engine(tmp, policy=False):
    r=DemonstrationRecorder(tmp/'data',tmp/'runtime',
        dict(purpose='policy_hil' if policy else 'demonstration',auto_video=False),
        'test',unified_buttons=True)
    calls=[]
    def infer(obs,rid,epoch,context):
        calls.append((obs,epoch,context))
        return np.zeros((context['coverage_steps'],16)),np.zeros((50,10))
    p=SimpleNamespace(model_id='synthetic',last_timing={},infer_rtc=infer,
                      held_getter=lambda:np.zeros(16),right_only=True) if policy else None
    if policy:
        from f1dagger.rtc_hil import RTCHILPolicyEngine
    cls=RTCHILPolicyEngine if policy else F1PolicyEngine
    opts=dict(duration_seconds=None,stop_requested=lambda:False,rtc_delay_steps=14,rtc_min_steps=16) if policy else {}
    e=cls(r.record_event,Source(),p,r,r,{'capture_hz':30.,'policy':{'chunk_steps':20,'frequency_hz':30.}},
          teleop_only=not policy,episodes=r,**opts)
    count=[0]
    def send(toggle,end=0,**kw):
        now=time.monotonic_ns();seq=count[0];count[0]+=1
        e.episode_buttons.poll(now)
        p=replace(packet(seq,toggle),episode_end_id=end,episode_end_pressed=False,**kw)
        e.operator(p,now,now)
        return p
    return e,r,calls,send


@pytest.mark.parametrize('policy',[False,True])
def test_two_episodes_up_saves_start_in_human_does_not_handoff(tmp_path,policy):
    e,r,calls,send=make_episode_engine(tmp_path,policy)
    ids=[]
    try:
        send(0)
        for start,end in [(1,1),(4,2)]:
            send(start, end-1)
            wait_for(lambda:r.phase=='recording')
            e.episode_buttons.poll(time.monotonic_ns())
            ids.append(r.run_id)
            if policy:
                deadline=time.monotonic()+2
                while not calls or calls[-1][1]!=e.supervisor.policy_epoch:
                    send(start,end-1);e.tick(time.monotonic_ns());time.sleep(.005)
                    assert time.monotonic()<deadline
                assert calls[-1][2]['bootstrap'] and calls[-1][2]['committed_actions16']==[]
                assert calls[-1][0].capture_started_monotonic_ns>=r.begin_ns
            send(start+1,end-1)
            assert e.supervisor.state is HILState.HIL_LATCHED
            send(start+2,end-1)
            assert r.phase=='recording' and e.supervisor.state is HILState.HIL_LATCHED
            time.sleep(.04)
            send(start+2,end)
            wait_for(lambda:r.phase=='idle')
            assert not e.started and not e.policy_enabled
            manifest=json.loads((r.root/ids[-1]/'journal/manifest.json').read_text())
            assert manifest['complete'] and manifest['counts']['observations']>0
            assert manifest['binding']['recording_boundary']=='f710_start_up_v2'
            send(start+2,end) # release/anchor while waiting; does not start.
            assert r.phase=='idle'
        assert ids[0]!=ids[1]
        assert len(list(r.root.iterdir()))==2
    finally:e.close();r.close()


@pytest.mark.parametrize('policy',[False,True])
def test_end_rejected_during_rb_home_and_start_never_queues(tmp_path,policy):
    e,r,_,send=make_episode_engine(tmp_path,policy)
    try:
        send(0);send(1);wait_for(lambda:r.phase=='recording');e.episode_buttons.poll(time.monotonic_ns())
        send(1,1,deadman=True);assert r.phase=='recording'
        b=e.episode_buttons;b.home_active=True
        send(1,2);assert r.phase=='recording'
        assert b.end_hint=='wait_for_home'
        b.home_active=False
        send(1,2);assert r.phase=='recording'  # rejected UP does not queue
        send(1,3,hil_button_pressed=True);assert r.phase=='recording'
        assert b.end_hint=='release_start'
        send(1,3);assert r.phase=='recording'
        send(1,4);wait_for(lambda:r.phase=='idle')
    finally:e.close();r.close()


@pytest.mark.parametrize('policy',[False,True],ids=['demonstration','rtc_human'])
@pytest.mark.parametrize('home_state',['never_used','just_finished','left_home'],
                         ids=['no_y','less_than_two_seconds','no_home_pose'])
def test_up_saves_without_home_pose_or_terminal_hold(tmp_path,policy,home_state):
    e,r,_,send=make_episode_engine(tmp_path,policy)
    try:
        send(0);send(1);wait_for(lambda:r.phase=='recording');e.episode_buttons.poll(time.monotonic_ns())
        toggle=2 if policy else 1
        send(toggle)
        assert e.supervisor.state is HILState.HIL_LATCHED
        b=e.episode_buttons
        if home_state!='never_used':
            b.home_goal=np.zeros(16)
            b.hold_since=time.monotonic_ns() if home_state=='just_finished' else None
        run=r.run_id
        send(toggle,1)
        wait_for(lambda:r.phase=='idle')
        assert r.run_id==run
        events=[json.loads(line) for line in (r.root/run/'journal/events.jsonl').read_text().splitlines()]
        saved=[row for row in events if row['event']=='episode_end_accepted']
        assert len(saved)==1 and saved[0]['reason']=='dpad_up' and not saved[0]['training_ready']
        assert saved[0]['home_completed']==(home_state!='never_used')
        if home_state=='just_finished':
            assert 0 <= saved[0]['terminal_hold_seconds'] < 2
        else:
            assert saved[0]['terminal_hold_seconds'] is None
        assert not any(row['event']=='episode_end_rejected' for row in events)
    finally:e.close();r.close()


@pytest.mark.parametrize('policy',[False,True],ids=['demonstration','rtc_human'])
@pytest.mark.parametrize('pending',['publication','new_target'])
def test_up_still_waits_for_gripper_and_requires_new_press(tmp_path,policy,pending):
    e,r,_,send=make_episode_engine(tmp_path,policy)
    try:
        send(0);send(1);wait_for(lambda:r.phase=='recording');e.episode_buttons.poll(time.monotonic_ns())
        previous=send(2 if policy else 1)
        fields=dict(sequence=previous.sequence+1,episode_end_id=1)
        if pending=='new_target':
            fields['grip_target']=0. if previous.grip_target!=0. else 100.
        p=replace(previous,**fields)
        now=time.monotonic_ns()
        e.operator(p,now,now,hand_pending=pending=='publication')
        assert r.phase=='recording' and e.episode_buttons.end_hint=='wait_for_gripper'
        p=replace(p,sequence=p.sequence+1)
        now=time.monotonic_ns();e.operator(p,now,now)
        assert r.phase=='recording'  # resolved condition does not replay old UP
        p=replace(p,sequence=p.sequence+1,episode_end_id=2)
        now=time.monotonic_ns();e.operator(p,now,now)
        wait_for(lambda:r.phase=='idle')
    finally:e.close();r.close()


def test_old_sender_and_reconnected_end_do_not_start_or_close(tmp_path):
    e,r,_,send=make_episode_engine(tmp_path)
    try:
        now=time.monotonic_ns();e.operator(packet(0,0),now,now)
        now=time.monotonic_ns();e.operator(packet(1,1),now,now)
        assert not e.started and r.phase=='idle'
        # A different session anchors counters, including UP already held.
        send(0,8,session_id='fresh');send(1,8,session_id='fresh')
        wait_for(lambda:r.phase=='recording');e.episode_buttons.poll(time.monotonic_ns())
        send(1,9,session_id='reconnected')
        assert r.phase=='recording'
    finally:e.close();r.close()


def test_old_chunk_and_events_cannot_leak_into_new_episode(tmp_path):
    e,r,_,send=make_episode_engine(tmp_path)
    try:
        send(0);send(1);wait_for(lambda:r.phase=='recording');e.episode_buttons.poll(time.monotonic_ns())
        old=observation(123,r.begin_ns+1);old_epoch=e.supervisor.policy_epoch
        send(1,1);wait_for(lambda:r.phase=='idle');send(1,1);send(2,1)
        wait_for(lambda:r.phase=='recording');e.episode_buttons.poll(time.monotonic_ns())
        r.record_chunk(old,old_epoch,91,1,2,np.zeros((50,16)),30.,'old')
        r.record_event('late_old_reply',policy_epoch=old_epoch,monotonic_ns=time.monotonic_ns())
        send(2,2);wait_for(lambda:r.phase=='idle')
        manifest=json.loads((r.root/r.run_id/'journal/manifest.json').read_text())
        assert manifest['counts']['policy_chunks']==0
        assert 'late_old_reply' not in (r.root/r.run_id/'journal/events.jsonl').read_text()
    finally:e.close();r.close()


def test_policy_rejection_stays_in_episode_for_manual_correction(tmp_path):
    e,r,_,send=make_episode_engine(tmp_path,True)
    try:
        send(0);send(1);wait_for(lambda:r.phase=='recording');e.episode_buttons.poll(time.monotonic_ns())
        e._policy_hold(time.monotonic_ns(),'ik_rejected:test')
        assert r.phase=='recording' and e.supervisor.state is HILState.SAFE_HOLD
        send(2)
        assert e.supervisor.state is HILState.HIL_LATCHED and e.hard_hold_reason is None
        send(3)
        assert e.supervisor.state is HILState.HIL_LATCHED
        send(3,1);wait_for(lambda:r.phase=='idle')
    finally:e.close();r.close()


def test_slow_open_and_save_consume_buttons_without_queued_start(tmp_path):
    import threading
    e,r,_,send=make_episode_engine(tmp_path,True)
    gate=threading.Event();original=r.factory
    def factory(path):
        assert gate.wait(2)
        return original(path)
    r.factory=factory
    try:
        send(0);send(1)
        assert r.phase=='starting' and not e.policy_enabled
        send(2);send(2,1)
        gate.set();wait_for(lambda:r.phase=='recording');e.episode_buttons.poll(time.monotonic_ns())
        assert r.phase=='recording'  # premature UP was consumed
        original_close=r.active[1].close
        gate.clear()
        def close(**kw):
            assert gate.wait(2)
            return original_close(**kw)
        r.active[1].close=close
        send(2,2);assert r.phase=='saving'
        send(3,2);send(3,2)
        gate.set();wait_for(lambda:r.phase=='idle')
        send(3,2);assert not e.started and r.phase=='idle'
        send(4,2);wait_for(lambda:r.phase=='recording')
    finally:gate.set();e.close();r.close()


def test_same_owner_holds_between_episodes_and_reanchors_every_archive(tmp_path):
    from f1dagger.runtime import ControlLoop
    from test_f1_output import make_owner
    e,r,_,_=make_episode_engine(tmp_path)
    o,t=make_owner()
    now=[time.monotonic_ns()]
    o.source.feedback=lambda:(np.zeros(22),o.last_submitted.copy(),(now[0],)*16)
    arm=SimpleNamespace(release=lambda:None,target=lambda p,f,last,dt:last+np.r_[np.zeros(7),.1,np.zeros(8)])
    loop=ControlLoop(e,o,arm,r,{'joint_tolerance_deg':1},clock=lambda:now[0])
    seq=[0]
    def step(toggle,end,**kw):
        now[0]=time.monotonic_ns()
        p=replace(packet(seq[0],toggle),episode_end_id=end,episode_end_pressed=False,**kw)
        seq[0]+=1;loop.step(now[0],(p,now[0]));time.sleep(.022)
    ids=[]
    try:
        step(0,0);step(1,0);wait_for(lambda:r.phase=='recording');step(1,0)
        step(1,0,deadman=True);step(1,0)
        b=e.episode_buttons;b.home_goal=o.last_submitted.copy();b.hold_since=now[0]-2_100_000_000
        # Terminal hold metadata still reflects measured feedback, but is no
        # longer a recording-completion gate chosen for the operator.
        o.source.feedback=lambda:(np.zeros(22),o.last_submitted.copy(),(1,)*16)
        step(1,0);assert r.phase=='recording' and b.hold_since is None
        o.source.feedback=lambda:(np.zeros(22),o.last_submitted.copy(),(now[0],)*16)
        step(1,0)
        ids.append(r.run_id);step(1,1);wait_for(lambda:r.phase=='idle')
        held=o.last_submitted.copy();before=len(t.sent)
        step(1,1,deadman=True);step(1,1)
        np.testing.assert_array_equal(held,o.last_submitted)
        assert len(t.sent)>before and r.phase=='idle'
        step(2,1);wait_for(lambda:r.phase=='recording');step(2,1);step(2,1)
        ids.append(r.run_id);step(2,2);wait_for(lambda:r.phase=='idle')
        for run in ids:
            rows=[json.loads(x) for x in (r.root/run/'journal/control.jsonl').read_text().splitlines()]
            assert rows and rows[0]['publications']
            assert {p['topic_key'] for p in rows[0]['publications']}=={'joint_command','left_gripper_command','right_gripper_command'}
    finally:e.close();r.close()

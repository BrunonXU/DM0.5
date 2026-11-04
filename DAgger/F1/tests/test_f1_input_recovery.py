"""No hardware: actual packet, episode writer, control loop and proof paths."""
from dataclasses import replace
import json
import time
import numpy as np
import pytest
from dagger_core.protocol import AckPacket, encode_packet, decode_intent, ProtocolError
from dagger_core.f710_mapping import GamepadSnapshot
from dagger_core.input_recovery import recovery_token, sampled_recovery_packet, without_recovery_token
from dagger_core.hil_supervisor import HILState
from f1dagger.input_recovery import RecoveryChallenges
from f1dagger.policy import F1PolicyEngine
from f1dagger.runtime import receiver_status
from test_f1_policy import Source, CONFIG
from test_f1_demonstration import recorder, wait_for
from test_f1_recording_audio import Cues
from test_f1_runtime import setup_loop
from test_real_observation_shadow import packet


def test_legacy_wire_unchanged_and_proof_bound_to_session_time_and_capacity():
    p = packet(0)
    fields = json.loads(encode_packet(p))
    assert 'recovery_token' not in fields and 'controls_neutral' not in fields
    proof = RecoveryChallenges(capacity=2)
    token = proof.issue(p.session_id, 100)
    p = replace(p, recovery_token=token, controls_neutral=True)
    assert decode_intent(encode_packet(p)) == p
    assert proof.age(p, 100, 150, 200) == 50
    for changed, after, now in [(p,101,150),(p,100,301),(p,100,99),
                                (replace(p, session_id='other'),100,150)]:
        assert proof.age(changed, after, now, 200) is None
    proof.issue('other',151);proof.issue('other',152)
    assert proof.age(p,100,153,200) is None
    with pytest.raises(ProtocolError):replace(p,controls_neutral=1)
    with pytest.raises(ProtocolError):replace(p,recovery_token='bad')


def test_raw_neutral_and_token_are_sampled_before_mapping_not_invented_from_zero_axes():
    token='a'*32
    ack=AckPacket('s',1,True,'HOLD','powered_hil:input_wait_release_rb;recovery_token='+token)
    assert recovery_token(ack)==token
    assert without_recovery_token(ack.reason)=='powered_hil:input_wait_release_rb'
    for sample in (GamepadSnapshot(left_x=.9),GamepadSnapshot(right_trigger=.8),
                   GamepadSnapshot(rb=True),GamepadSnapshot(y=True),GamepadSnapshot(dpad_x=1)):
        p=sampled_recovery_packet(packet(0),token,sample,.18)
        assert not p.controls_neutral and p.axes==(0.,0.,0.)
    assert sampled_recovery_packet(packet(0),token,GamepadSnapshot(),.18).controls_neutral
    assert sampled_recovery_packet(packet(0),None,GamepadSnapshot(),.18).recovery_token is None


@pytest.mark.parametrize('wait_status', [
    'input_wait_release_rb', 'startup_wait_operator', 'policy_wait_operator'])
def test_loopback_negotiation_and_bounded_transport_telemetry(wait_status):
    from dagger_core.async_transport import AsyncLatestIntentTransport
    from f1dagger.network import IntentServer
    server=IntentServer(0)
    sender=AsyncLatestIntentTransport('127.0.0.1',server.sock.getsockname()[1],status=lambda _:None)
    try:
        server.snapshot(wait_status);sender.start()
        wait_for(lambda:sender.snapshot().connected)
        generation=sender.snapshot().generation
        after=time.monotonic_ns()
        assert sender.publish(generation,packet(0))
        wait_for(lambda:sender.snapshot().last_ack is not None)
        ack=sender.snapshot().last_ack
        token=recovery_token(ack)
        assert token
        p=sampled_recovery_packet(packet(1),token,GamepadSnapshot(),.18)
        assert sender.publish(generation,p)
        wait_for(lambda:sender.snapshot().last_ack.sequence==1)
        incoming,_=server.snapshot('input_wait_press_rb')
        assert incoming[0].controls_neutral
        assert server.recovery.age(incoming[0],after,time.monotonic_ns(),200_000_000) is not None
        metrics=sender.snapshot()
        assert len(metrics.publish_to_worker_ms)==len(metrics.socket_write_ms)==2
        assert all(x>=0 for x in metrics.publish_to_worker_ms+metrics.socket_write_ms)
    finally:sender.close();server.close()


def recovery_loop():
    loop,e,o,t,now,records,step=setup_loop()
    proof=RecoveryChallenges();e.recovery_proof=proof.age
    e.source.feedback=lambda:(np.zeros(22),np.zeros(16),(now[0],)*16)
    o.source.feedback=lambda:(np.zeros(22),o.last_submitted.copy(),(now[0],)*16)
    step(0);step(1,1);step(2,1,deadman=True)
    now[0]+=210_000_000;loop.step(now[0])
    assert e.input_pause and e.supervisor.state is HILState.SAFE_HOLD
    def send(seq, **kw):
        token=proof.issue(e.packet.session_id,now[0])
        step(seq,1,recovery_token=token,**kw)
    return loop,e,o,t,now,records,step,send,proof


def test_held_rb_old_packets_home_and_rt_cannot_resume_then_fresh_release_press_can():
    loop,e,o,t,now,records,step,send,proof=recovery_loop()
    try:
        held=o.last_submitted.copy();epoch=e.supervisor.policy_epoch
        for i in range(3,7):send(i,deadman=True,grip_target=0.)
        assert e.input_pause and loop.home is None
        np.testing.assert_array_equal(o.last_submitted,held)
        send(7,home_reset=True)
        assert loop.home is None
        send(8,controls_neutral=True)
        assert receiver_status(e)=='input_wait_fresh_packet'
        send(9,controls_neutral=True)
        assert receiver_status(e)=='input_wait_press_rb'
        send(10,deadman=True,grip_target=0.)
        assert e.input_pause is None and e.supervisor.state is HILState.HIL_LATCHED
        assert e.supervisor.policy_epoch>epoch and records[-1]['source']=='input_recovery_anchor'
        np.testing.assert_array_equal(o.last_submitted[14:],held[14:])
        assert loop.home is None
    finally:e.close()


@pytest.mark.parametrize('fault',['no_token','old_token','pre_pause','feedback','gap'])
def test_failed_resume_never_rearms_on_later_held_rb(fault):
    loop,e,o,t,now,records,step,send,proof=recovery_loop()
    try:
        send(3,controls_neutral=True)
        token=proof.issue(e.packet.session_id,now[0])
        if fault=='no_token':token=None
        if fault=='old_token':now[0]+=210_000_000
        if fault=='pre_pause':token=proof.issue(e.packet.session_id,e.input_pause['started']-1)
        if fault=='feedback':e.source.feedback=lambda:(np.zeros(22),np.zeros(16),(1,)*16)
        if fault=='gap':now[0]+=210_000_000;token=proof.issue(e.packet.session_id,now[0])
        step(4,1,deadman=True,recovery_token=token)
        assert e.input_pause
        e.source.feedback=lambda:(np.zeros(22),np.zeros(16),(now[0],)*16)
        send(5,deadman=True)
        assert e.input_pause
        send(6,controls_neutral=True);send(7,deadman=True)
        assert e.input_pause is None
    finally:e.close()


def test_arriving_packet_cannot_hide_a_gap_between_control_ticks():
    loop,e,o,t,now,records,step=setup_loop()
    try:
        step(0);step(1,1);step(2,1,deadman=True)
        held=o.last_submitted.copy()
        now[0]+=210_000_000
        step(3,1,deadman=True)
        assert e.input_pause
        np.testing.assert_array_equal(o.last_submitted,held)
    finally:e.close()


def test_same_episode_survives_timeout_and_saves_only_explicit_start_then_next_episode(tmp_path):
    cues=Cues();r=recorder(tmp_path,audio=cues)
    events=[]
    def emit(name,**fields):
        events.append((name,fields));assert r.record_event(name,**fields)
    e=F1PolicyEngine(emit,Source(),None,r,r,CONFIG,teleop_only=True,episodes=r)
    proof=RecoveryChallenges();e.recovery_proof=proof.age
    e.source.feedback=lambda:(np.zeros(22),np.zeros(16),(time.monotonic_ns()-1,)*16)
    def send(seq,toggle=1,**kw):
        now=time.monotonic_ns()
        token=proof.issue(e.packet.session_id if e.packet else 'unused',now)
        e.operator(replace(packet(seq,toggle),recovery_token=token,**kw),now,now+1)
    try:
        send(0,0);send(1);wait_for(lambda:r.phase=='recording')
        run=r.run_id
        time.sleep(.22);e.tick(time.monotonic_ns())
        assert e.input_pause and r.run_id==run and r.phase=='recording'
        time.sleep(.15)
        assert cues.events==['started']
        send(2,controls_neutral=True);send(3,deadman=True)
        assert e.input_pause is None and r.run_id==run
        send(4);send(5,2)
        wait_for(lambda:r.phase=='idle')
        assert cues.events==['started','saved']
        manifest=json.loads((tmp_path/'data'/run/'journal/manifest.json').read_text())
        assert manifest['complete'] and manifest['counts']['observations']>=5
        assert any(n=='input_pause_started' for n,_ in events)
        assert any(n=='input_pause_ended' for n,_ in events)
        send(6,3);wait_for(lambda:r.phase=='recording')
        assert r.run_id!=run
        e.disconnect(time.monotonic_ns(),'camera_unavailable:test')
        wait_for(lambda:r.phase=='idle')
        assert cues.events==['started','saved','started']
        saved=json.loads((tmp_path/'data'/r.run_id/'journal/manifest.json').read_text())
        assert saved['binding']['termination']['outcome']=='interrupted'
    finally:e.close();r.close()


def test_start_during_pause_explicitly_finishes_not_resumes(tmp_path):
    r=recorder(tmp_path);e=F1PolicyEngine(lambda *a,**k:None,Source(),None,r,r,CONFIG,teleop_only=True,episodes=r)
    proof=RecoveryChallenges();e.recovery_proof=proof.age
    try:
        now=time.monotonic_ns();e.operator(packet(0),now,now);e.operator(packet(1,1),now+1,now+1)
        wait_for(lambda:r.phase=='recording')
        pause_at=now+210_000_000;e.tick(pause_at)
        token=proof.issue(e.packet.session_id,pause_at+1)
        e.operator(replace(packet(2,2),recovery_token=token),pause_at+2,pause_at+2)
        wait_for(lambda:r.phase=='idle')
        assert e.input_pause is None and e.supervisor.state is HILState.HANDOFF_HOLD
    finally:e.close();r.close()


@pytest.mark.parametrize('fault',['invalid_operator_packet:decode','target_rejected:collision','camera_unavailable:timeout'])
def test_hard_fault_escalates_pause_and_cannot_use_rb(fault):
    loop,e,o,t,now,records,step,send,proof=recovery_loop()
    try:
        e.disconnect(now[0],fault)
        assert e.input_pause is None and e.supervisor.state is HILState.SAFE_HOLD
        send(3,controls_neutral=True);send(4,deadman=True)
        assert e.supervisor.state is HILState.SAFE_HOLD
    finally:e.close()


def test_export_cuts_entire_uncertain_interval_including_pre_watchdog_tail(tmp_path):
    from test_f1_recording import raw_episode
    from f1dagger.export import select_segments, input_invalid_intervals
    events=[dict(event='input_pause_started',monotonic_ns=1_400_000_000,
                 invalid_from_monotonic_ns=1_200_000_000),
            dict(event='input_pause_ended',monotonic_ns=1_600_000_000,outcome='rb_rearmed')]
    run,review=raw_episode(tmp_path,['human_arm']*31,fps=30,events=events)
    groups,report,_=select_segments(run,json.loads(review.read_text()))
    assert len(groups)==2
    assert groups[0][-1]['reference_monotonic_ns']<1_200_000_000
    assert groups[1][0]['reference_monotonic_ns']>1_600_000_000
    assert report['input_invalid_intervals']==[(1_200_000_000,1_600_000_000)]
    assert input_invalid_intervals(events[:1])==[(1_200_000_000,None)]

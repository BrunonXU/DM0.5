import json
import time
from dataclasses import replace
import threading

from f1dagger.demonstration import DemonstrationRecorder
from f1dagger.policy import F1PolicyEngine
from dagger_core.hil_supervisor import HILState
from test_f1_policy import Source, CONFIG, observation
from test_real_observation_shadow import packet


def wait_for(check):
    end = time.monotonic()+5
    while not check():
        assert time.monotonic()<end
        time.sleep(.01)


def recorder(tmp_path, **kw):
    return DemonstrationRecorder(tmp_path/'data', tmp_path/'runtime',
        dict(purpose='demonstration', recording_mode='hil-record', auto_video=False), 'test', **kw)


def test_idle_writes_no_data_and_two_episodes_commit_independently(tmp_path):
    r=recorder(tmp_path)
    ctx=dict(hil_state='hil_latched', intervention_id=1, policy_epoch=1)
    try:
        for i in range(10):
            assert r.submit(observation(i))
            assert r.record_timestamped_observation(observation(i), **ctx)
        assert not (tmp_path/'data').exists()
        ids=[]
        for i in range(2):
            assert r.start(time.monotonic_ns())
            wait_for(lambda:r.phase=='recording')
            ids.append(r.run_id)
            assert r.record_timestamped_observation(observation(100, r.begin_ns-1), **ctx)
            assert r.record_timestamped_observation(observation(i, r.begin_ns+1), **ctx)
            r.finish(time.monotonic_ns())
            assert r.record_timestamped_observation(observation(101), **ctx)
            wait_for(lambda:r.phase=='idle')
            run=tmp_path/'data'/ids[-1]
            j=json.loads((run/'journal/manifest.json').read_text())
            p=json.loads((run/'payload/manifest.json').read_text())
            assert j['complete'] and p['complete']
            assert j['counts']['observations']==p['count']==1
            assert r.last_saved['phase']=='saved'
        assert ids[0]!=ids[1]
    finally:assert r.close()


def test_engine_start_stop_and_reconnect_keep_recording_boundaries(tmp_path):
    r=recorder(tmp_path)
    e=F1PolicyEngine(lambda *a,**kw:None, Source(), None, r, r, CONFIG, teleop_only=True, episodes=r)
    def send(seq,toggle,**kw):
        now=time.monotonic_ns()
        e.operator(replace(packet(seq,toggle),**kw),now,now)
    try:
        send(0,0)
        assert r.phase=='idle' and not (tmp_path/'data').exists()
        send(1,1)
        wait_for(lambda:r.phase=='recording')
        send(2,2,deadman=True)  # Existing RB protection is retained.
        assert r.phase=='recording' and e.supervisor.state is HILState.HIL_LATCHED
        send(3,3)
        wait_for(lambda:r.phase=='idle')
        assert e.supervisor.state is HILState.HANDOFF_HOLD
        send(4,4)
        wait_for(lambda:r.phase=='recording')
        e.disconnect(time.monotonic_ns())
        wait_for(lambda:r.phase=='idle')
        assert e.supervisor.state is HILState.SAFE_HOLD
        send(5,4)
        assert r.phase=='idle'  # Reconnect/ordinary packets cannot restart.
        send(6,5)
        wait_for(lambda:r.phase=='recording')
    finally:e.close();assert r.close()


def test_stop_during_slow_creation_does_not_auto_start(tmp_path):
    entered,release=threading.Event(),threading.Event()
    r=recorder(tmp_path)
    create=r.factory
    def delayed(run):
        entered.set();assert release.wait(3);return create(run)
    r.factory=delayed
    try:
        r.start(time.monotonic_ns());assert entered.wait(1)
        r.finish(time.monotonic_ns())
        assert r.phase=='saving' and not r.start(time.monotonic_ns())
        release.set()
        wait_for(lambda:r.phase=='idle')
        assert r.active is None
    finally:release.set();assert r.close()


def test_failed_writer_is_not_saved_or_restarted(tmp_path):
    r=recorder(tmp_path, factory=lambda run:(_ for _ in ()).throw(OSError('disk full')))
    r.start(time.monotonic_ns())
    wait_for(lambda:r.phase=='failed')
    assert r.writer_error=='disk full'
    assert not r.start(time.monotonic_ns())
    assert not r.close()


def test_control_waits_for_writer_and_each_episode_anchors_gripper_publications(tmp_path):
    from test_f1_runtime import setup_loop
    loop,e,o,t,now,records,step=setup_loop()
    r=recorder(tmp_path)
    # Gate a real control-loop fake transport without blocking its ticks.
    e.episodes=r
    entered,release=threading.Event(),threading.Event()
    create=r.factory
    def delayed(run):
        entered.set();assert release.wait(3);return create(run)
    r.factory=delayed
    try:
        step(0);step(1,1)
        assert entered.wait(1)
        step(2,1,deadman=True)
        assert loop.source=='hold' and o.last_submitted[0]==0
        release.set();wait_for(lambda:r.phase=='recording')
        step(3,1)
        keys=[p['topic_key'] for p in records[-1]['publications']]
        assert keys==['joint_command','left_gripper_command','right_gripper_command']
        step(4,2)
        wait_for(lambda:r.phase=='idle')
        step(5,3);wait_for(lambda:r.phase=='recording')
        step(6,3)
        assert [p['topic_key'] for p in records[-1]['publications']]==keys
    finally:release.set();e.close();assert r.close()


def test_first_frame_preserves_cumulative_drop_provenance(tmp_path):
    from f1dagger.observation import CAMERA_IDENTITY
    r=recorder(tmp_path)
    try:
        r.start(time.monotonic_ns());wait_for(lambda:r.phase=='recording')
        obs=replace(observation(30,r.begin_ns+1),camera_identity_source=CAMERA_IDENTITY,
                    camera_drop_counts=(10,2,1))
        assert r.record_timestamped_observation(obs,hil_state='hil_latched',intervention_id=1,policy_epoch=1)
        r.finish(time.monotonic_ns());wait_for(lambda:r.phase=='idle')
        run=tmp_path/'data'/r.run_id
        j=json.loads((run/'journal/observations.jsonl').read_text())
        p=json.loads((run/'payload/frames.jsonl').read_text())
        assert j['camera_discontinuity'] is p['camera_discontinuity'] is True
        assert j['camera_drop_counts']==p['camera_drop_counts']==dict(head=10,left_wrist=2,right_wrist=1)
    finally:assert r.close()


def test_two_control_episodes_pass_raw_audit(tmp_path):
    from test_f1_runtime import setup_loop
    from f1dagger.recording import audit
    loop,e,o,t,now,records,step=setup_loop()
    r=recorder(tmp_path)
    e.episodes=e.recorder=e.sink=loop.recorder=r
    try:
        step(0)
        seq=1
        for toggle in (1,3):
            step(seq,toggle);seq+=1
            wait_for(lambda:r.phase=='recording')
            run=tmp_path/'data'/r.run_id
            for _ in range(8):
                step(seq,toggle);seq+=1;time.sleep(.01)
            step(seq,toggle+1);seq+=1
            wait_for(lambda:r.phase=='idle')
            result=audit(run)
            assert result['valid'],result
            assert result['counts']['control']>0 and result['counts']['observations']>0
    finally:e.close();assert r.close()


def test_runtime_status_keeps_episode_phase_and_last_saved(tmp_path):
    from f1dagger.runtime import RuntimeReporter
    r=recorder(tmp_path)
    e=F1PolicyEngine(lambda *a,**kw:None,Source(),None,r,r,CONFIG,teleop_only=True,episodes=r)
    lines=[]
    try:
        reporter=RuntimeReporter('shadow',r,emit=lambda line,**kw:lines.append(json.loads(line.removeprefix('F1 STATUS '))))
        reporter.report(time.monotonic_ns(),e,None)
        assert lines[-1]['state']=='demonstration_ready'
        assert lines[-1]['recording']['phase']=='idle'
        assert lines[-1]['recording']['last_saved'] is None
    finally:e.close();assert r.close()


def test_home_stop_keeps_same_episode_and_start_can_save_and_start_next(tmp_path,monkeypatch):
    import f1dagger.runtime as runtime
    from f1dagger.clearance_home import ClearanceHomeStopped
    from test_f1_runtime import setup_loop
    class Failed:
        stage='lift'
        def retry_context(self):return None
        def propose(self,*args):raise ClearanceHomeStopped('recorded wrist tracking error')
    monkeypatch.setattr(runtime,'ClearanceHomeMotion',Failed)
    loop,e,o,t,now,records,step=setup_loop();r=recorder(tmp_path)
    e.episodes=e.recorder=e.sink=loop.recorder=r
    try:
        step(0);step(1,1);wait_for(lambda:r.phase=='recording')
        step(2,1);run_id=r.run_id
        loop.home=o.last_submitted.copy();loop.home_motion=Failed()
        step(3,1)
        assert loop.home is None and e.supervisor.state is HILState.HIL_LATCHED
        assert r.phase=='recording' and r.run_id==run_id
        # Continue writing to the SAME episode after stopping only Home.
        step(4,1);step(5,2);wait_for(lambda:r.phase=='idle')
        run=tmp_path/'data'/run_id
        events=[json.loads(x) for x in (run/'journal/events.jsonl').read_text().splitlines()]
        assert any(x['event']=='home_stopped' for x in events)
        assert not any(x['event']=='target_rejected' for x in events)
        assert r.last_saved['phase']=='saved'
        step(6,3);wait_for(lambda:r.phase=='recording');step(7,3)
        assert r.run_id!=run_id and loop.home is None
        # START must also finish a recording while a Home is still active.
        loop.home=o.last_submitted.copy();loop.home_motion=Failed()
        step(8,4);wait_for(lambda:r.phase=='idle')
        assert e.supervisor.state is HILState.HANDOFF_HOLD and loop.home is None
        assert r.last_saved['phase']=='saved'
    finally:e.close();assert r.close()

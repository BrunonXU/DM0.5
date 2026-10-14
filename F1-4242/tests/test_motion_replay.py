"""Publication fidelity and failure boundaries with fake ROS and a fake clock."""
import hashlib
import json
from pathlib import Path
import sys
from dataclasses import replace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from robot.replay_data import ARMS, CONTEXT, NAMES, KEYS, read_recording
from robot.replay_action import validate, review
from test_pose_action import setup, live_feedback_event
from test_ros_adapter import Event, joints
from test_data_auth import http_app


def source(root, count=31, mutate=None):
    root.mkdir(parents=True, exist_ok=True)
    folder = root/'journal'; folder.mkdir(exist_ok=True)
    controls, observations = [], []
    stamps = [None]*16
    for i in range(count):
        ns = 1000000000+i*10000000
        q = [i*.01]+[0.]*13+[10., 30. if i >= 10 else 10.]
        publications = [dict(topic_key=KEYS[0], monotonic_ns=ns, publisher_gid='01'*16)]
        stamps[:14] = [ns]*14
        for j in (14, 15):
            if i == 0 or (j == 15 and i == 10):
                t = ns+(j-13)*1000; stamps[j] = t
                publications.append(dict(topic_key=KEYS[j-13], monotonic_ns=t, publisher_gid='02'*16))
        controls.append(dict(episode_id='fixture', stream_sequence=i, command_sequence=20+i,
                             monotonic_ns=ns+3000, submitted=q, publications=publications,
                             action_source_timestamps_ns=list(stamps), publication_complete=True,
                             execution_mode='powered', source='human'))
        observations.append(dict(episode_id='fixture', stream_sequence=i, available_monotonic_ns=ns+4000,
                                 full22=q[:14]+[0.,0.,0.,0.,0.,-10.,-20.,-10.]))
    events = [dict(episode_id='fixture', stream_sequence=0, monotonic_ns=1100000000,
                   event='home_stopped', reason='fixture tracking error')]
    manifest = dict(schema_version=3, episode_id='fixture', contract='f1_upper_16d_v1',
                    complete=True, status='completed', drop_count=0,
                    action_contract=dict(dimension=16, names=list(NAMES), units=['degree']*14+['0..100']*2),
                    binding=dict(purpose='demonstration', execution_mode='powered', robot='F1-09', profile_sha256='a'*64))
    streams = dict(control=controls, observations=observations, events=events)
    if mutate:
        mutate(manifest, streams)
    manifest['counts'] = {k:len(v) for k, v in streams.items()}
    manifest['stream_sha256'] = {}
    for name, rows in streams.items():
        raw = ''.join(json.dumps(r)+'\n' for r in rows).encode()
        (folder/(name+'.jsonl')).write_bytes(raw)
        manifest['stream_sha256'][name] = hashlib.sha256(raw).hexdigest()
    (folder/'manifest.json').write_text(json.dumps(manifest))
    return root


def replay_setup(tmp_path, count=31):
    a, clock, transport = setup()
    a.profile.update(profile_sha256='a'*64, robot_id='F1-09')
    segment = read_recording(source(tmp_path/'recording', count)).select()
    spec = dict(kind='replay', _segment=segment,
                replay=dict(episode_id='e'*24), _report_path=str(tmp_path/'result.json'))
    return a, clock, transport, segment, spec


def test_reader_preserves_times_events_and_gripper_publications(tmp_path):
    rec = read_recording(source(tmp_path/'source'))
    assert len(rec.commands) == 31
    assert len([p for r in rec.commands for p in r.publications if p[0]==KEYS[1]]) == 1
    assert len([p for r in rec.commands for p in r.publications if p[0]==KEYS[2]]) == 2
    seg = rec.select(.08, .22)
    assert seg.commands[0].sequence == 28 and seg.commands[-1].sequence == 42
    assert seg.events[0]['selected'] and seg.events[0]['time_s'] == .1
    assert seg.summary()['time_basis'] == 'first_command_publication'


@pytest.mark.parametrize('mutation', [
    lambda m,s: m['binding'].update(execution_mode='shadow'),
    lambda m,s: m.update(complete=False),
    lambda m,s: s['control'][1].update(submitted=None),
    lambda m,s: s['control'][1].update(publication_complete=False),
    lambda m,s: s['control'][1]['submitted'].__setitem__(14, 90),
    lambda m,s: s['control'][1].update(command_sequence=20),
    lambda m,s: s['control'][1]['publications'][0].update(publisher_gid='00'*16),
    lambda m,s: s['control'][1]['publications'][0].update(topic_key='base_command'),
    lambda m,s: s['control'][1]['submitted'].__setitem__(0, float('nan')),
    lambda m,s: s['observations'][0].update(full22=None),
    lambda m,s: s['events'][0].update(episode_id='foreign'),
])
def test_untrusted_or_incomplete_evidence_rejected(tmp_path, mutation):
    with pytest.raises((ValueError, TypeError)):
        read_recording(source(tmp_path/'source', mutate=mutation))


def test_changed_stream_and_symlink_refused(tmp_path):
    root = source(tmp_path/'source')
    p = root/'journal/control.jsonl'; p.write_bytes(p.read_bytes()+b'\n')
    with pytest.raises(ValueError, match='SHA256'):
        read_recording(root)
    root = source(tmp_path/'source')
    original = tmp_path/'outside';p.rename(original);p.symlink_to(original)
    with pytest.raises(ValueError, match='软链接'):
        read_recording(root)


@pytest.mark.parametrize('start,end', [(True,None),(-1,None),(0,float('inf')),(.2,.1),(0,99)])
def test_invalid_intervals(tmp_path, start, end):
    with pytest.raises(ValueError):
        read_recording(source(tmp_path/'source')).select(start, end)


def test_review_has_no_output_and_replay_is_faithful(tmp_path):
    a,c,t,seg,spec = replay_setup(tmp_path)
    assert review(a,seg)['max_discrete_command_speed_deg_s'] == pytest.approx(1)
    assert not t.sent and not t.prepared
    ticks = []; publish = t.publish
    t.publish = lambda k,v: (ticks.append((c(),k,v)),publish(k,v))[-1]
    result = a.execute(spec, live_feedback_event(a,c))
    assert result['status']=='succeeded' and result['feedback_verified']
    arm = [v for _,k,v in ticks if k==KEYS[0]]
    assert [v['arm_l_j1'] for v in arm] == [r.values[0] for r in seg.commands]
    assert len([k for _,k,_ in ticks if k==KEYS[1]]) == 1
    assert len([k for _,k,_ in ticks if k==KEYS[2]]) == 2
    actual_times = [at for at,k,_ in ticks if k==KEYS[0]]
    assert [at-actual_times[0] for at in actual_times] == pytest.approx([i*.01 for i in range(31)])
    assert set(t.released)==set(KEYS)
    report = json.loads(Path(spec['_report_path']).read_text())
    assert report['result']['feedback_verified'] and report['samples']


def test_crop_seeds_held_gripper_once_after_start_check(tmp_path):
    a,c,t,seg,spec = replay_setup(tmp_path)
    seg = seg.recording.select(.15,.25);spec['_segment']=seg
    a.store.update_gripper('right',joints(['right_gripper_position'],[30]))
    a.execute(spec,live_feedback_event(a,c))
    assert len([k for k,_ in t.sent if k==KEYS[1]])==1
    assert len([k for k,_ in t.sent if k==KEYS[2]])==1


@pytest.mark.parametrize('problem', ['start','context','profile','robot','limits','source_movement','gap','budget'])
def test_preflight_rejects_before_creating_endpoints(tmp_path, problem):
    a,c,t,seg,spec = replay_setup(tmp_path)
    if problem=='start':a.store.update_joints(joints(['arm_l_j1'],[20]))
    if problem=='context':a.store.update_joints(joints(['head_j1'],[20]))
    if problem=='profile':a.profile['profile_sha256']='b'*64
    if problem=='robot':a.profile['robot_id']='other'
    if problem=='limits':a.profile['joint_limits_deg']['arm_l_j1']=[-10,.1]
    if problem=='source_movement':spec['_segment']=replace(seg,observations=((seg.begin,tuple([20]+list(seg.context[1:]))),))
    if problem=='gap':a.profile['body_max_command_gap_s']=.005
    if problem=='budget':a.profile['max_replay_task_s']=1
    with pytest.raises((ValueError, RuntimeError)):
        a.execute(spec,Event(c))
    assert not t.sent and not t.prepared


def test_changed_start_during_prepare_is_rechecked(tmp_path):
    a,c,t,seg,spec=replay_setup(tmp_path)
    prepare=t.prepare
    def changed(key):
        prepare(key);a.store.update_joints(joints(['arm_l_j1'],[10]))
    t.prepare=changed
    with pytest.raises(ValueError,match='起点'):
        a.execute(spec,Event(c))
    assert not t.sent and set(t.released)==set(KEYS)


def test_publication_derivatives_do_not_masquerade_as_motor_acceleration(tmp_path):
    a,c,t,seg,spec=replay_setup(tmp_path)
    rows=list(seg.commands)
    rows[4]=replace(rows[4],values=(.05,)+rows[4].values[1:])
    seg=replace(seg,commands=tuple(rows));spec['_segment']=seg
    diagnostic=validate(a.profile,seg)
    assert diagnostic['max_discrete_command_acceleration_deg_s2'] > a.profile['max_joint_acceleration_deg_s2']
    result=a.execute(spec,live_feedback_event(a,c))
    assert result['feedback_verified']
    assert [v['arm_l_j1'] for k,v in t.sent if k==KEYS[0]]==[r.values[0] for r in rows]


@pytest.mark.parametrize('problem', ['external','stale','context'])
def test_running_feedback_and_exclusivity_failures_stop_output(tmp_path, problem):
    a,c,t,seg,spec=replay_setup(tmp_path)
    publish=t.publish
    def fail(key,value):
        publish(key,value)
        if len(t.sent)==6:
            if problem=='external':t.conflict=True
            if problem=='stale':a.store.joints.pop('arm_l_j1')
            if problem=='context':a.store.update_joints(joints(['head_j1'],[20]))
    t.publish=fail
    with pytest.raises((RuntimeError,ValueError)):
        a.execute(spec,live_feedback_event(a,c))
    assert set(t.released)==set(KEYS)
    assert len([k for k,_ in t.sent if k==KEYS[0]]) < len(seg.commands)


def test_cancellation_and_late_scheduling_do_not_skip_or_resume(tmp_path):
    a,c,t,seg,spec=replay_setup(tmp_path)
    result=a.execute(spec,live_feedback_event(a,c,after=c()+.055))
    assert result['status']=='cancelled' and result['hold_sent'] and not result['feedback_verified']
    assert len([k for k,_ in t.sent if k==KEYS[0]]) < 31
    a,c,t,seg,spec=replay_setup(tmp_path/'late')
    class Delayed(Event):
        def wait(self,seconds):self.clock.now+=.04;return False
    with pytest.raises(ValueError,match='节拍|错过'):
        a.execute(spec,Delayed(c))
    assert len([k for k,_ in t.sent if k==KEYS[0]])==2 # first + measured hold


def test_fresh_but_stalled_feedback_is_failure(tmp_path):
    a,c,t,seg,spec=replay_setup(tmp_path, count=301)
    t.mirror=False
    with pytest.raises(ValueError,match='未跟上'):
        a.execute(spec,live_feedback_event(a,c))
    report=json.loads(Path(spec['_report_path']).read_text())
    assert report['result']['status']=='failed' and not report['result']['feedback_verified']


def gateway_source(app, monkeypatch, tmp_path):
    a,c,t,seg,spec=replay_setup(tmp_path)
    app.adapter=a;app.actions.adapter=a
    root=tmp_path/'recording'
    monkeypatch.setattr(app.hil_data,'locate',lambda eid:(root,dict(source='hil_demonstration',source_signature='source')))
    return a,c,t


def test_http_read_only_review_and_authenticated_one_shot_execution(http_app, monkeypatch, tmp_path):
    app,request=http_app;a,c,t=gateway_source(app,monkeypatch,tmp_path)
    _,_,cookie=request('/api/session')
    route='/api/replay/review?episode_id='+'e'*24
    status,response,_=request(route,cookie=cookie)
    assert status==200 and response['review']['executable']
    assert not t.sent and app.lease.fd is None and app.actions.latest() is None
    body=dict(review_id=response['review']['review_id'],confirm=True,acknowledge_events=True)
    assert request('/api/replay/start',body,cookie)[0]==401
    _,auth,_=request('/api/control/acquire',dict(control_code='fixture-control-code'),cookie)
    body['lease_id']=auth['lease_id']
    assert request('/api/replay/start',body,cookie,origin=False)[0]==403
    assert request('/api/replay/start',{**body,'confirm':False},cookie)[0]==409
    calls=[]
    monkeypatch.setattr(a,'execute',lambda spec,cancel,progress:calls.append(spec) or dict(status='cancelled'))
    status,response,_=request('/api/replay/start',body,cookie)
    assert status==200
    app.actions.thread.join(1)
    assert response['action']['replay']['episode_id']=='e'*24 and len(calls)==1
    assert request('/api/replay/start',body,cookie)[0]==409
    assert request('/api/actions',dict(kind='replay',lease_id=auth['lease_id']),cookie)[0]==409


@pytest.mark.parametrize('change',['source','profile','expiry','other_session','busy'])
def test_http_review_binding_invalidates_changed_inputs(http_app,monkeypatch,tmp_path,change):
    app,request=http_app;a,c,t=gateway_source(app,monkeypatch,tmp_path)
    _,auth,cookie=request('/api/control/acquire',dict(control_code='fixture-control-code'))
    _,response,_=request('/api/replay/review?episode_id='+'e'*24,cookie=cookie)
    token=response['review']['review_id'];assert token
    if change=='source':
        path=tmp_path/'recording/journal/control.jsonl';path.write_text(path.read_text()+'\n')
    if change=='profile':a.profile['max_joint_speed_deg_s']=20
    if change=='expiry':app.replay.tickets[token]['expires']=0
    if change=='busy':monkeypatch.setattr(app.hil,'state',lambda:dict(active=True))
    if change=='other_session':
        _,_,cookie=request('/api/session');request('/api/login',dict(token=app.token),cookie)
    status,_,_=request('/api/replay/start',dict(review_id=token,confirm=True,acknowledge_events=True,lease_id=auth['lease_id']),cookie)
    assert status==409 and app.actions.latest() is None and not t.sent


@pytest.mark.parametrize('operation', ['stop','release','expiry'])
def test_replay_lease_covers_output_cleanup_and_never_restarts(http_app,monkeypatch,tmp_path,operation):
    import threading
    import time
    from core import ControlLease
    from test_pose_task_lifecycle import eventually
    app,request=http_app;a,c,t=gateway_source(app,monkeypatch,tmp_path)
    entered,cancelled,cleanup=(threading.Event() for _ in range(3))
    calls=[]
    def execute(spec,cancel,progress):
        calls.append(spec);entered.set()
        assert cancel.wait(3)
        cancelled.set();assert cleanup.wait(3)
        return dict(status='cancelled',feedback_verified=False,commands_sent=True)
    monkeypatch.setattr(a,'execute',execute)
    _,auth,cookie=request('/api/control/acquire',dict(control_code='fixture-control-code'))
    _,checked,_=request('/api/replay/review?episode_id='+'e'*24,cookie=cookie)
    body=dict(review_id=checked['review']['review_id'],lease_id=auth['lease_id'],confirm=True,acknowledge_events=True)
    external=ControlLease(app.lease.path)
    try:
        assert request('/api/replay/start',body,cookie)[0]==200 and entered.wait(1)
        if operation=='expiry':
            with app.op_lock:app.lease.deadline=time.monotonic()-1
        else:
            route='/api/stop' if operation=='stop' else '/api/control/release'
            assert request(route,dict(lease_id=auth['lease_id']),cookie)[0]==200
        assert cancelled.wait(1) and app.actions.busy()
        with pytest.raises(ValueError):external.acquire('other')
        assert request('/api/replay/start',body,cookie)[0]==409
        cleanup.set();app.actions.thread.join(1)
        assert app.actions.latest()['status']=='cancelled' and len(calls)==1
        if operation!='stop':eventually(lambda:app.lease.fd is None)
    finally:
        cleanup.set();app.actions.stop(wait=1);external.release()


def test_failed_recheck_invalidates_previous_ticket(http_app,monkeypatch,tmp_path):
    app,request=http_app;a,c,t=gateway_source(app,monkeypatch,tmp_path)
    _,_,cookie=request('/api/session')
    _,checked,_=request('/api/replay/review?episode_id='+'e'*24,cookie=cookie)
    token=checked['review']['review_id'];assert token in app.replay.tickets
    a.store.update_joints(joints(['arm_l_j1'],[20]))
    _,checked,_=request('/api/replay/review?episode_id='+'e'*24,cookie=cookie)
    assert not checked['review']['executable'] and token not in app.replay.tickets


def test_stop_during_source_verification_prevents_later_start(http_app,monkeypatch,tmp_path):
    import threading
    app,request=http_app;a,c,t=gateway_source(app,monkeypatch,tmp_path)
    _,auth,cookie=request('/api/control/acquire',dict(control_code='fixture-control-code'))
    _,checked,_=request('/api/replay/review?episode_id='+'e'*24,cookie=cookie)
    entered,finish=threading.Event(),threading.Event();responses=[]
    load=app.replay.load
    def slow(*args):
        entered.set();assert finish.wait(3);return load(*args)
    monkeypatch.setattr(app.replay,'load',slow)
    body=dict(review_id=checked['review']['review_id'],lease_id=auth['lease_id'],confirm=True,acknowledge_events=True)
    thread=threading.Thread(target=lambda:responses.append(request('/api/replay/start',body,cookie)))
    thread.start()
    try:
        assert entered.wait(1)
        assert request('/api/stop',dict(lease_id=auth['lease_id']),cookie)[0]==200
    finally:
        finish.set();thread.join(2)
    assert responses[0][0]==409 and app.actions.latest() is None and not t.sent


def test_schedule_construction_time_is_not_playback_lateness(tmp_path, monkeypatch):
    import robot.replay_action as replay
    a,c,t,seg,spec=replay_setup(tmp_path, count=10309)
    prepare=replay.prepare_schedule
    def slow(segment):
        c.now += .020
        return prepare(segment)
    monkeypatch.setattr(replay,'prepare_schedule',slow)
    result=a.execute(spec,live_feedback_event(a,c,after=c()+.065))
    assert result['commands_sent'] and result['status']=='cancelled'
    assert result['dispatch_timing']['max_lateness_ms'] == pytest.approx(0,abs=1e-5)


def test_read_feedback_timestamps_and_compare_original_not_target(tmp_path):
    from robot.replay_compare import compare
    def observed(manifest, streams):
        for r in streams['observations']:
            t=r['available_monotonic_ns']
            r.update(state16=[2.]*14+[0.,100.],state_timestamps_ns=[t]*16)
    seg=read_recording(source(tmp_path/'source',mutate=observed)).select()
    samples=[dict(actual=[3.]*14+[0.,90.],feedback_times_s=[t]*16) for t in [.02,.04,.06]]
    result=compare(seg,samples,max_gap_s=.25,execution_status='succeeded')
    assert result['available']
    assert result['channels'][0]['metrics']['mean_abs']==1
    assert result['channels'][15]['metrics']['max_abs']==10
    assert result['channels'][0]['matched']==3
    assert result['channels'][0]['points'][0][1]==2 # recorded measurement, not original command


def test_comparison_refuses_missing_feedback_and_gaps(tmp_path):
    from robot.replay_compare import compare
    seg=read_recording(source(tmp_path/'source')).select()
    samples=[dict(actual=[1.]*16,feedback_times_s=[.1]*16)]
    assert not compare(seg,samples,max_gap_s=.25,execution_status='failed')['available']
    series=tuple(((seg.begin,0.),(seg.begin+1000000000,2.)) for _ in range(16))
    seg=replace(seg,recording=replace(seg.recording,state_series=series))
    report=compare(seg,samples,max_gap_s=.25,execution_status='failed')
    assert report['channels'][0]['missing']==1 and not report['available']
    assert report['channels'][0]['metrics'] is None


def test_alignment_bound_to_source_confirmed_and_never_starts_replay(http_app,monkeypatch,tmp_path):
    app,request=http_app;a,c,t=gateway_source(app,monkeypatch,tmp_path)
    a.store.update_joints(joints(['arm_l_j1'],[10]))
    _,auth,cookie=request('/api/control/acquire',dict(control_code='fixture-control-code'))
    _,data,_=request('/api/replay/review?episode_id='+'e'*24,cookie=cookie)
    checked=data['review'];assert checked['can_align'] and not checked['executable']
    body=dict(review_id=checked['review_id'],lease_id=auth['lease_id'],confirm=True)
    assert request('/api/replay/start',body,cookie)[0]==409
    assert request('/api/replay/align',{**body,'confirm':False},cookie)[0]==409
    assert request('/api/replay/align',body,cookie,origin=False)[0]==403
    calls=[]
    monkeypatch.setattr(a,'execute',lambda spec,cancel,progress:calls.append(spec) or dict(status='succeeded'))
    assert request('/api/replay/align',body,cookie)[0]==200
    app.actions.thread.join(1)
    assert len(calls)==1 and calls[0]['kind']=='pose'
    assert calls[0]['replay']['phase']=='alignment'
    assert calls[0]['targets']['arm_l_j1']==0
    assert request('/api/replay/align',body,cookie)[0]==409
    assert not t.sent


def test_report_read_is_bounded_and_does_not_accept_paths(http_app,tmp_path):
    app,request=http_app
    root=app.runtime/'replay_runs';root.mkdir()
    report_id='a'*32
    (root/(report_id+'.json')).write_text(json.dumps(dict(result={'status':'failed'},samples=[{}],comparison={})))
    code,data,_=request('/api/replay/reports/'+report_id)
    assert code==200 and 'samples' not in data['report']
    (root/('b'*32+'.json')).symlink_to(root/(report_id+'.json'))
    assert request('/api/replay/reports/'+'b'*32)[0]!=200
    assert request('/api/replay/reports/not-a-report')[0]!=200


def test_submillisecond_source_spacing_records_jitter_without_dropping_commands(tmp_path,monkeypatch):
    import robot.replay_action as replay
    a,c,t,seg,spec=replay_setup(tmp_path)
    rows=list(seg.commands);row=rows[1];ns=seg.begin+900000
    rows[1]=replace(row,ns=ns+3000,publications=((KEYS[0],ns),))
    spec['_segment']=replace(seg,commands=tuple(rows))
    original=replay.live_check
    def checked(*args,**kwargs):
        value=original(*args,**kwargs)
        if kwargs.get('cached'):c.now+=.001
        return value
    monkeypatch.setattr(replay,'live_check',checked)
    result=a.execute(spec,live_feedback_event(a,c))
    assert result['feedback_verified']
    assert result['dispatch_timing']['max_lateness_ms']>1
    assert [v['arm_l_j1'] for k,v in t.sent if k==KEYS[0]]==[r.values[0] for r in rows]


def test_alignment_recovery_requires_specific_ack(http_app,monkeypatch,tmp_path):
    import robot.pose_review as planner
    app,request=http_app;a,c,t=gateway_source(app,monkeypatch,tmp_path)
    a.store.update_joints(joints(['arm_l_j1'],[10]))
    monkeypatch.setattr(planner,'review',lambda *args:dict(executable=True,plan={'head_boundary_recovery':{'joint':'head_j2'}},issues=[]))
    _,auth,cookie=request('/api/control/acquire',dict(control_code='fixture-control-code'))
    route='/api/replay/review?episode_id='+'e'*24
    _,data,_=request(route,cookie=cookie)
    body=dict(review_id=data['review']['review_id'],lease_id=auth['lease_id'],confirm=True)
    assert request('/api/replay/align',body,cookie)[0]==409
    assert not t.sent and app.actions.latest() is None
    _,data,_=request(route,cookie=cookie);body['review_id']=data['review']['review_id']
    calls=[]
    monkeypatch.setattr(a,'execute',lambda spec,cancel,progress:calls.append(spec) or dict(status='succeeded'))
    assert request('/api/replay/align',{**body,'head_boundary_recovery_ack':True},cookie)[0]==200
    app.actions.thread.join(1)
    assert calls[0]['head_boundary_recovery_ack'] is True and calls[0]['kind']=='pose'

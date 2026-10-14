import sys,math
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_body_stream import stream
from test_ros_adapter import Event,joints
from robot.adapter import HEAD_NAMES,ActionRejected

def setup():
    a,c,t=stream();a.profile['joint_limits_deg'].update(dict(zip(HEAD_NAMES,[[-40,40],[-25,25],[-15,15]])))
    a.store.update_joints(joints(list(HEAD_NAMES),[0,0,0]));a.store.update_gripper('right',joints(['right_gripper_position'],[10]))
    original=t.assert_exclusive
    def refresh(key):
        original(key)
        # Simulate incoming full HAL frames even for stationary joints.
        row=a.store.snapshot()['joints'];a.store.update_joints(joints(list(row),[v['position'] for v in row.values()]))
        for side,v in a.store.snapshot()['grippers'].items():a.store.update_gripper(side,joints([side+'_gripper_position'],[v['position']]))
    t.assert_exclusive=refresh;t.assert_exclusive_cached=refresh
    return a,c,t

def test_full_pose_invalid_head_rejects_every_part_before_preparing():
    a,c,t=setup();a.store.update_joints(joints(['head_j2'],[-27.6]))
    with pytest.raises(ActionRejected,match='head_j2'):a.execute({'kind':'pose','targets':{'arm_l_j1':10,'body_j3':-15,'head_j2':0}},Event(c))
    assert not t.sent and not t.prepared

def live_feedback_event(a,c,**kwargs):
    class Incoming(Event):
        def wait(self,seconds):
            result=super().wait(seconds)
            row=a.store.snapshot()['joints']
            a.store.update_joints(joints(list(row),[v['position'] for v in row.values()]))
            for side,v in a.store.snapshot()['grippers'].items():
                a.store.update_gripper(side,joints([side+'_gripper_position'],[v['position']]))
            return result
    return Incoming(c,**kwargs)


def test_noop_pose_sends_nothing_and_reports_all_parts():
    a,c,t=setup();r=a.execute({'kind':'pose','targets':{'arm_l_j1':.1,'head_j2':0},'grippers':{'left':10}},live_feedback_event(a,c))
    assert r['no_motion'] and r['status']=='succeeded' and not r['commands_sent'] and len(r['parts'])==3
    assert r['feedback_verified'] and r['elapsed_s']>=.15
    assert not t.prepared and not t.sent

def test_all_parts_share_time_and_preserve_body_vs_arm_rates():
    a,c,t=setup();times=[];publish=t.publish
    def output(k,v):times.append((c(),k,v.copy() if isinstance(v,dict) else v));publish(k,v)
    t.publish=output
    spec={'kind':'pose','targets':{'arm_l_j1':15,'arm_r_j1':-15,'body_j1':5,'head_j2':5},'grippers':{'left':30,'right':40}}
    p=a.plan(spec);assert len(p['parts'])==6 and p['mode']=='synchronized' and not t.sent
    r=a.execute(spec,Event(c));assert r['status']=='succeeded' and all(x['status']=='at_target' for x in r['parts']) and r['elapsed_s']<6
    arm=[(at,v['arm_l_j1']) for at,k,v in times if k=='joint_command' and 'arm_l_j1' in v]
    body=[at for at,k,v in times if k=='joint_command' and 'body_j1' in v]
    assert max(b-a for a,b in zip(body,body[1:]))==pytest.approx(.005)
    assert max(b[0]-a[0] for a,b in zip(arm,arm[1:]))<=.035001
    assert max(abs(b[1]-a[1])/(b[0]-a[0]) for a,b in zip(arm,arm[1:]))<=30
    assert abs(arm[-1][1]-15)<1e-8

def test_long_pose_executes_once_at_fixed_speed_with_internal_checkpoint():
    a,c,t=setup();before=c();rows=[];publish=t.publish;progress=[]
    def capture(k,v):rows.append((c()-before,k,dict(v) if isinstance(v,dict) else v));publish(k,v)
    t.publish=capture
    spec={'kind':'pose','targets':{'body_j3':-65,'arm_l_j1':10}}
    plan=a.plan(spec)
    assert 6<plan['motion_s']<10 and plan['checkpoint_budget_s']==6
    result=a.execute(spec,Event(c),progress.append)
    assert result['status']=='succeeded' and result['feedback_verified'] and result['checkpoint_checks']==1
    assert 6<result['elapsed_s']<plan['max_action_s'] and t.prepared==['joint_command']
    body=[(at,v['body_j3']) for at,k,v in rows if k=='joint_command']
    velocities=[(b[1]-x[1])/(b[0]-x[0]) for x,b in zip(body,body[1:])]
    assert max(map(abs,velocities))<=15.0001
    # No hold, velocity reset, second publisher or user action at the checkpoint.
    crossing=[abs(velocities[i]) for i in range(len(velocities)) if 5.65<body[i][0]<5.75]
    assert min(crossing)>1 and max(crossing)-min(crossing)<1
    assert max(b[0]-x[0] for x,b in zip(body,body[1:]))<=.005001
    assert all(b['progress']>=x['progress'] for x,b in zip(progress,progress[1:]))
    assert progress[-1]['feedback_arrived']


def test_cancel_stops_whole_pose_and_reports_parts():
    a,c,t=setup();r=a.execute({'kind':'pose','targets':{'body_j1':10,'arm_l_j1':20},'grippers':{'left':30}},Event(c,after=c()+.2))
    assert r['status']=='cancelled' and r['hold_sent'] and len(r['parts'])==3
    assert set(t.released)=={'joint_command','left_gripper_command'}

def test_stalled_part_cannot_be_reported_as_full_success():
    a,c,t=setup();original=t.publish
    def stalled(key,v):
        if key=='joint_command':v={n:x for n,x in v.items() if n!='head_j2'}
        original(key,v)
    t.publish=stalled
    with pytest.raises(ValueError,match='全部到位|未跟上') as e:a.execute({'kind':'pose','targets':{'arm_l_j1':10,'head_j2':5}},Event(c))
    assert e.value.commands_sent and any(x['id']=='head' and x['status']!='at_target' for x in e.value.parts)

def test_pose_relative_height_and_arm_together():
    a,c,t=setup();a.profile['waist_cartesian_enabled']=True
    a.store.update_joints(joints(['body_j3','body_j4','body_j5'],[-30,-50,-20]))
    r=a.execute({'kind':'pose','targets':{'arm_l_j1':10},'waist_delta_m':.02},Event(c))
    assert r['status']=='succeeded' and len(r['parts'])==2

from test_data_auth import http_app

def test_pose_http_preview_needs_no_lease_and_is_readonly(http_app,monkeypatch):
    import urllib.parse,json
    app,request=http_app;calls=[]
    monkeypatch.setattr(app.adapter,'plan',lambda x:(calls.append(x) or {'parts':[]}),raising=False)
    spec={'kind':'pose','targets':{'head_j2':2,'arm_l_j1':5}}
    assert request('/api/pose/preview?spec='+urllib.parse.quote(json.dumps(spec)))[0]==200
    assert calls==[spec] and app.lease.fd is None and not app.actions.busy()

def test_invalid_gripper_prevents_valid_arm_output():
    a,c,t=setup()
    with pytest.raises(ValueError,match='夹爪'):a.execute({'kind':'pose','targets':{'arm_l_j1':10},'grippers':{'left':101}},Event(c))
    assert not t.prepared and not t.sent

def test_state_change_during_preparation_rejects_entire_pose():
    a,c,t=setup();prepare=t.prepare
    def changed(key):
        prepare(key);a.store.update_joints(joints(['head_j2'],[-27.6]))
    t.prepare=changed
    with pytest.raises(ActionRejected,match='head_j2'):a.execute({'kind':'pose','targets':{'arm_l_j1':10,'head_j2':5}},Event(c))
    assert not t.sent and t.released==['joint_command']

def test_turn_with_relative_height_preserves_both_goals():
    a,c,t=setup();a.profile['waist_cartesian_enabled']=True
    a.store.update_joints(joints(['body_j3','body_j4','body_j5'],[-30,-50,-20]))
    r=a.execute({'kind':'pose','targets':{'body_j1':10},'waist_delta_m':.02},Event(c));assert r['status']=='succeeded'
    assert a.store.snapshot()['joints']['body_j1']['position']==pytest.approx(10)


def test_stalled_fresh_part_is_stopped_at_internal_checkpoint():
    a,c,t=setup();original=t.publish;started=c()
    def stalled(key,value):
        if key=='joint_command':value={n:v for n,v in value.items() if n!='body_j3'}
        original(key,value)
    t.publish=stalled
    with pytest.raises(ValueError,match='未跟上') as error:
        a.execute({'kind':'pose','targets':{'body_j3':-65,'arm_l_j1':100}},Event(c))
    assert 5.7<=c()-started<6 and error.value.commands_sent and error.value.hold_sent
    assert t.released==['joint_command'] and not a._action_lock.locked()


def test_long_pose_cancel_after_six_seconds_never_resumes():
    a,c,t=setup();started=c()
    result=a.execute({'kind':'pose','targets':{'body_j3':-65,'arm_l_j1':100}},Event(c,after=started+6.2))
    assert result['status']=='cancelled' and result['hold_sent'] and not result['feedback_verified']
    assert 6.2-1e-9<=c()-started<6.21
    assert a.store.snapshot()['joints']['body_j3']['position']>-65 and t.released==['joint_command']


def test_external_source_after_checkpoint_aborts_without_later_output():
    a,c,t=setup();started=c();times=[];publish=t.publish
    def capture(k,v):times.append(c()-started);publish(k,v)
    t.publish=capture
    class Conflict(Event):
        def wait(self,seconds):
            self.clock.now+=seconds
            if self.clock()-started>=6.2:t.conflict=True
            return False
    with pytest.raises(ActionRejected,match='external'):
        a.execute({'kind':'pose','targets':{'body_j3':-65}},Conflict(c))
    assert max(times)<6.2 and t.released==['joint_command']


def test_long_task_stale_feedback_aborts_after_first_checkpoint():
    a,c,t=setup();started=c();original=t.assert_exclusive
    def stale(key):
        original(key)
        if c()-started>=6.2:a.store.joints['body_j3']['received_at']=c()-1
    t.assert_exclusive=t.assert_exclusive_cached=stale
    with pytest.raises(ActionRejected,match='过期') as error:
        a.execute({'kind':'pose','targets':{'body_j3':-65}},Event(c))
    assert error.value.commands_sent and not error.value.hold_sent and c()-started<6.21


def test_selected_stationary_part_is_monitored_but_not_published():
    a,c,t=setup();r=a.execute({'kind':'pose','targets':{'body_j3':-65,'head_j2':0}},Event(c))
    assert r['status']=='succeeded' and {p['id'] for p in r['parts']}=={'head','body'}
    assert not any(n.startswith('head_') for k,v in t.sent if k=='joint_command' for n in v)
    a,c,t=setup();started=c();original=t.assert_exclusive_cached
    def drift(key):
        original(key)
        if c()-started>4:a.store.update_joints(joints(['head_j2'],[5]))
    t.assert_exclusive_cached=drift
    with pytest.raises(ValueError,match='头部未跟上'):
        a.execute({'kind':'pose','targets':{'body_j3':-65,'head_j2':0}},Event(c))
    assert c()-started<6 and not any(n.startswith('head_') for k,v in t.sent if k=='joint_command' for n in v)


def test_pose_total_ceiling_cannot_be_overridden_by_request():
    a,c,t=setup();a.profile['body_speed_deg_s']=.1
    spec={'kind':'pose','targets':{'body_j3':-65},'max_pose_task_s':3600,'duration_s':1,'max_action_s':3600}
    with pytest.raises(ActionRejected,match='整套任务时限 60'):
        a.execute(spec,Event(c))
    assert not t.prepared and not t.sent


def test_finish_deadline_uses_planned_time_not_sixty_second_ceiling():
    a,c,t=setup();started=c();original=t.publish
    def stall_late(key,value):
        if c()-started>6 and key=='joint_command':value={n:v for n,v in value.items() if n!='body_j3'}
        original(key,value)
    t.publish=stall_late
    spec={'kind':'pose','targets':{'body_j3':-70}}
    deadline=a.plan(spec)['max_action_s']
    with pytest.raises(ValueError,match='全部到位'):
        a.execute(spec,Event(c))
    assert deadline<=c()-started<deadline+.01 and c()-started<12


def test_checkpoints_continue_during_final_feedback_wait():
    a,c,t=setup();started=c();original=t.publish
    def stall_after_checkpoint(key,value):
        if c()-started>5.8 and key=='joint_command':value={n:v for n,v in value.items() if n!='body_j1'}
        original(key,value)
    t.publish=stall_after_checkpoint
    spec={'kind':'pose','targets':{'body_j1':85}}
    assert a.plan(spec)['motion_s']==pytest.approx(10.625)
    with pytest.raises(ValueError,match='未跟上'):
        a.execute(spec,Event(c))
    # The checkpoint at11.4 s still runs although motion ended at10.625 s;
    # failure is not postponed until the total13.625 s arrival deadline.
    assert 11.4-1e-9<=c()-started<11.41


def test_long_pose_retimes_waist_ik_without_changing_height_intent():
    from robot.waist_kinematics import forward
    a,c,t=setup();a.profile['waist_cartesian_enabled']=True
    origin={'body_j3':-30,'body_j4':-50,'body_j5':-20}
    a.store.update_joints(joints(list(origin),list(origin.values())))
    x,z,pitch=forward(origin)
    spec={'kind':'pose','targets':{'arm_l_j1':150,'body_j1':10},'waist_delta_m':.02}
    plan=a.plan(spec)
    assert plan['motion_s']==pytest.approx(9.375)
    result=a.execute(spec,Event(c))
    assert result['status']=='succeeded' and result['checkpoint_checks']==1
    for key,values in t.sent:
        if key!='joint_command':continue
        xx,zz,pp=forward(values)
        assert xx==pytest.approx(x) and pp==pytest.approx(pitch)
    final=a.store.snapshot()['joints']
    assert forward({n:r['position'] for n,r in final.items()})[1]==pytest.approx(z+.02)
    assert final['arm_l_j1']['position']==pytest.approx(150) and final['body_j1']['position']==pytest.approx(10)


def test_noop_requires_new_samples_and_does_not_verify_a_cached_frame():
    a,c,t=setup()
    def check_source(key):
        a.store.graph_at=c()
        if t.conflict:raise ActionRejected('unknown external publisher')
    t.assert_exclusive=check_source;t.assert_exclusive_cached=check_source
    with pytest.raises(ActionRejected,match='过期') as error:
        a.execute({'kind':'pose','targets':{'arm_l_j1':0}},Event(c))
    assert not error.value.commands_sent and not t.prepared and not t.sent


def test_noop_timestamp_refresh_without_new_receipt_cannot_verify_arrival():
    a,c,t=setup()
    def check_source(key):
        a.store.graph_at=c()
        if t.conflict:raise ActionRejected('unknown external publisher')
    t.assert_exclusive=check_source;t.assert_exclusive_cached=check_source
    class ReusedFrame(Event):
        def wait(self,seconds):
            result=super().wait(seconds)
            for row in a.store.joints.values():row['received_at']=c()
            a.store.graph_at=c()
            return result
    with pytest.raises(ValueError,match='连续到位反馈'):
        a.execute({'kind':'pose','targets':{'arm_l_j1':0}},ReusedFrame(c))
    assert not t.prepared and not t.sent


def test_noop_cancel_during_confirmation_is_not_success():
    a,c,t=setup()
    result=a.execute({'kind':'pose','targets':{'arm_l_j1':0}},live_feedback_event(a,c,after=c()+.1))
    assert result['status']=='cancelled' and not result['feedback_verified']
    assert not t.prepared and not t.sent


def test_noop_drift_does_not_create_a_publisher_or_move_without_new_plan():
    a,c,t=setup()
    class Drift(Event):
        def wait(self,seconds):
            result=super().wait(seconds)
            a.store.update_joints(joints(['head_j2'],[5]))
            return result
    with pytest.raises(ValueError,match='实测姿态已改变') as error:
        a.execute({'kind':'pose','targets':{'head_j2':0}},Drift(c))
    assert error.value.parts==[{'id':'head','label':'头部','status':'moving'}]
    assert not t.prepared and not t.sent


def test_arrival_during_endpoint_preparation_still_requires_observation():
    a,c,t=setup();prepare=t.prepare
    def arrive(key):
        prepare(key);a.store.update_joints(joints(['arm_l_j1'],[5]))
    t.prepare=arrive
    result=a.execute({'kind':'pose','targets':{'arm_l_j1':5}},live_feedback_event(a,c))
    assert result['no_motion'] and result['feedback_verified'] and result['elapsed_s']>=.15
    assert t.prepared==t.released==['joint_command'] and not t.sent


def test_cancel_from_arrival_progress_callback_cannot_report_success():
    a,c,t=setup();event=live_feedback_event(a,c)
    def stop_on_arrival(row):
        if row['feedback_arrived']:event.set()
    result=a.execute({'kind':'pose','targets':{'head_j2':0}},event,stop_on_arrival)
    assert result['status']=='cancelled' and not result['feedback_verified'] and not t.sent
    a,c,t=setup();event=Event(c)
    result=a.execute({'kind':'pose','targets':{'head_j2':5}},event,stop_on_arrival)
    assert result['status']=='cancelled' and not result['feedback_verified'] and result['hold_sent']


def test_cancel_does_not_publish_an_out_of_range_measured_hold():
    a,c,t=setup()
    class ExceptionalFeedback(Event):
        def wait(self,seconds):
            super().wait(seconds)
            a.store.update_joints(joints(['head_j2'],[-27.64]))
            self.set()
            return True
    result=a.execute({'kind':'pose','targets':{'head_j2':5}},ExceptionalFeedback(c))
    assert result['status']=='cancelled' and result['commands_sent']
    assert not result['hold_sent'] and not result['feedback_verified']
    assert len(t.sent)==1 and all(-25<=row['head_j2']<=25 for key,row in t.sent if key=='joint_command')

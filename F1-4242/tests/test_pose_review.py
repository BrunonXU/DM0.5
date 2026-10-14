import copy,json,urllib.parse
import pytest
from test_pose_action import setup
from test_ros_adapter import joints,Event
from robot.pose_review import review
from robot.adapter import ActionRejected
from test_data_auth import http_app


def test_invalid_head_blocks_whole_long_task_without_user_segments():
    a,c,t=setup();a.store.update_joints(joints(['head_j2','body_j4'],[-27.64,-74.26]))
    result=review(a,{'kind':'pose','targets':{'head_j2':0,'body_j4':-10,'arm_l_j1':15}})
    assert not result['executable'] and result['segments']==[]
    assert {x['part']:x['code'] for x in result['issues']}=={'head':'origin_out_of_range'}
    assert not t.sent and not t.prepared


def test_long_review_preserves_all_parts_without_mutation_or_manual_segments():
    a,c,t=setup();a.store.update_joints(joints(['body_j4'],[-74.26]))
    profile=copy.deepcopy(a.profile);state=copy.deepcopy(a.store.snapshot()['joints'])
    spec={'kind':'pose','targets':{'body_j4':-10,'head_j2':5,'arm_l_j1':15},'grippers':{'left':40}}
    result=review(a,spec)
    assert result['executable'] and result['segments']==[] and result['issues']==[]
    plan=result['plan']
    assert plan['motion_s']==pytest.approx(8.0325) and plan['checkpoint_budget_s']==6
    assert {x['id'] for x in plan['parts']}=={'body','head','left_arm','left_gripper'}
    assert a.profile==profile and {n:v['position'] for n,v in a.store.snapshot()['joints'].items()}=={n:v['position'] for n,v in state.items()}
    assert not t.sent and not t.prepared
    out=a.execute(spec,Event(c))
    assert out['status']=='succeeded' and out['checkpoint_checks']==1 and out['feedback_verified']
    assert a.store.snapshot()['joints']['body_j4']['position']==pytest.approx(-10)
    assert a.store.snapshot()['joints']['head_j2']['position']==pytest.approx(5)


def test_legacy_joint_execution_retains_six_second_budget():
    a,c,t=setup();a.store.update_joints(joints(['body_j4'],[-74.26]))
    targets={'body_j4':-10}
    assert review(a,{'kind':'pose','targets':targets})['executable']
    with pytest.raises(ActionRejected,match='6 秒'):a.execute({'kind':'joints','targets':targets},Event(c))
    assert not t.prepared and not t.sent


def test_geometry_rejection_hides_segments_even_if_budget_was_first(monkeypatch):
    a,c,t=setup();a.store.update_joints(joints(['body_j4'],[-74.26]));a.profile['ground_guard']=True
    import robot.adapter
    monkeypatch.setattr(robot.adapter,'check_ground_path',lambda *args:(_ for _ in ()).throw(ValueError('模型路径穿过地面')))
    result=review(a,{'kind':'pose','targets':{'body_j4':-10}})
    assert result['segments']==[] and any('穿过地面' in x['message'] for x in result['issues'])
    assert not t.sent and not t.prepared


def test_external_source_or_stale_feedback_never_get_segment_plan():
    a,c,t=setup();a.store.update_joints(joints(['body_j4'],[-74.26]))
    def conflict(k):raise ActionRejected('其他控制源')
    t.assert_exclusive=conflict
    result=review(a,{'kind':'pose','targets':{'body_j4':-10}})
    assert not result['segments'] and any('其他控制源' in x['message'] for x in result['issues'])
    c.now+=10
    with pytest.raises(ActionRejected):review(a,{'kind':'pose','targets':{'body_j4':-10}})
    assert not t.sent and not t.prepared


def test_head_inward_recovery_is_not_faked_by_widening_or_clamping():
    a,c,t=setup();a.store.update_joints(joints(['head_j2'],[-27.64]))
    original=copy.deepcopy(a.profile)
    result=review(a,{'kind':'pose','targets':{'head_j2':0}})
    assert not result['executable'] and result['issues'][0]['code']=='origin_out_of_range'
    assert a.profile==original and not t.sent and not t.prepared
    with pytest.raises(ActionRejected):a.execute({'kind':'pose','targets':{'head_j2':0}},Event(c))


def test_review_http_is_readonly_without_lease(http_app,monkeypatch):
    app,request=http_app;a,c,t=setup();monkeypatch.setattr(app,'adapter',a)
    a.store.update_joints(joints(['head_j2','body_j4'],[-27.64,-74.26]))
    spec={'kind':'pose','targets':{'head_j2':0,'body_j4':-10}}
    status,data,_=request('/api/pose/review?spec='+urllib.parse.quote(json.dumps(spec)))
    assert status==200 and len(data['review']['issues'])==1
    assert app.lease.fd is None and not app.actions.busy() and not t.prepared and not t.sent

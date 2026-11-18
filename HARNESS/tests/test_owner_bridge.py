import threading
import time
from f1_harness.host import fake_host
from f1_harness.owner_bridge import OwnerBridge, JointProposal
from f1_harness.contracts import SkillIntent, digest


def install_running(engine, request_id):
    obs=engine.observation
    engine.running=SkillIntent(request_id=request_id,session_id=engine.session,owner_instance_id=engine.instance,
        task_id=engine.task.task_id,task_hash=digest(engine.task),epoch=engine.epoch,
        observation_id=obs.observation_id,scene_version=obs.scene_version,
        calibration_id=engine.task.calibration_id,skill='approach_grasp')


def test_slow_numerics_never_block_tick_and_late_ik_is_discarded(tmp_path):
    e,p,_,_=fake_host(tmp_path)
    entered=threading.Event();release=threading.Event()
    def planner(intent,obs,full,held):
        entered.set();release.wait(2)
        return JointProposal(intent.request_id,intent.epoch,intent.scene_version,intent.calibration_id,
                             full,(held,),.1,10)
    bridge=OwnerBridge(e,numerical_planner=planner,result_verifier=lambda *a:None,
                       feedback_age_ns=1_000_000_000,reference_tolerance_deg=.5)
    obs=p.observe()
    intent=SkillIntent(request_id='slow',session_id=e.session,owner_instance_id=e.instance,
        task_id=e.task.task_id,task_hash=digest(e.task),epoch=e.epoch,observation_id=obs.observation_id,
        scene_version=obs.scene_version,calibration_id=e.task.calibration_id,skill='approach_grasp')
    e.submit_skill(intent.model_dump())
    def poll():return bridge.poll(full22=(0.,)*22,measured16=(0.,)*16,stamps16=(e.clock(),)*16,held16=(0.,)*16)
    try:
        started=time.monotonic();assert poll() is None
        assert time.monotonic()-started<.1
        assert entered.wait(1)
        bridge.revoke('manual_takeover',preempted=True)
        release.set()
        for _ in range(10):assert poll() is None
        assert e.results['slow'].lifecycle=='preempted'
    finally:release.set();bridge.close()


def test_cancel_stationary_window_detects_cumulative_slow_drift(tmp_path):
    e,_,_,_=fake_host(tmp_path)
    bridge=OwnerBridge(e,numerical_planner=lambda *a:None,result_verifier=lambda *a:None,
        feedback_age_ns=1_000_000_000,reference_tolerance_deg=.5,stop_delta_deg=.01,stop_window_ns=200)
    bridge.revoke('test_cancel')
    try:
        for index in range(12):
            stamp=100+index*50
            values=[0.]*16;values[7]=index*.004
            bridge.feedback_snapshot={'stamps16':[stamp]*16}
            bridge.after_publication(publication_id=str(index),measured16=values,publication_complete=True,now_ns=stamp)
            assert e.hold_requested  # Per-tick drift is small, accumulated drift exceeds threshold.
        for index in range(6):
            stamp=1000+index*50
            bridge.feedback_snapshot={'stamps16':[stamp]*16}
            bridge.after_publication(publication_id='stable',measured16=values,publication_complete=True,now_ns=stamp)
        assert not e.hold_requested
    finally:bridge.close()


def test_closed_arm_does_not_advance_before_gripper_arrives(tmp_path):
    e,_,_,_=fake_host(tmp_path)
    install_running(e,'grip')
    bridge=OwnerBridge(e,numerical_planner=lambda *a:None,result_verifier=lambda *a:None,
        feedback_age_ns=1000,reference_tolerance_deg=.5)
    target=[0.]*16;target[15]=60.
    bridge.proposal=JointProposal('grip',e.epoch,'scene','fake',(0.,)*22,(tuple(target),tuple(target)),.1,100,1.)
    bridge.current_target=target
    try:
        for stamp,actual in [(100,0.),(300,40.),(500,59.5)]:
            measured=target.copy();measured[15]=actual
            bridge.update_feedback(full22=(0.,)*22,measured16=measured,stamps16=(stamp,)*16)
            bridge.after_publication(publication_id=str(stamp),measured16=measured,publication_complete=True,now_ns=stamp)
            assert bridge.index==0
        bridge.update_feedback(full22=(0.,)*22,measured16=target,stamps16=(650,)*16)
        bridge.after_publication(publication_id='arrived',measured16=target,publication_complete=True,now_ns=650)
        assert bridge.index==1
    finally:bridge.close()


def test_stale_gripper_cancels_even_with_fresh_arm(tmp_path):
    e,_,_,_=fake_host(tmp_path)
    install_running(e,'grip')
    bridge=OwnerBridge(e,numerical_planner=lambda *a:None,result_verifier=lambda *a:None,
        feedback_age_ns=100,reference_tolerance_deg=.5)
    target=(0.,)*16
    bridge.proposal=JointProposal('grip',e.epoch,'scene','fake',(0.,)*22,(target,),.1,10,1.)
    bridge.current_target=target
    stamps=[1000]*16;stamps[15]=1
    try:
        bridge.update_feedback(full22=(0.,)*22,measured16=target,stamps16=stamps)
        bridge.after_publication(publication_id='stale',measured16=target,publication_complete=True,now_ns=1000)
        assert not e.authorized and e.hold_requested
        assert bridge.index==0
    finally:bridge.close()


def test_cancel_waits_for_gripper_stationarity(tmp_path):
    e,_,_,_=fake_host(tmp_path)
    bridge=OwnerBridge(e,numerical_planner=lambda *a:None,result_verifier=lambda *a:None,
        feedback_age_ns=1000,reference_tolerance_deg=.5,stop_window_ns=200,stop_gripper_delta=.1)
    bridge.revoke('cancel')
    try:
        for i in range(10):
            now=100+i*50;values=[0.]*16;values[15]=i*.06
            bridge.update_feedback(full22=(0.,)*22,measured16=values,stamps16=(now,)*16)
            bridge.after_publication(publication_id=str(i),measured16=values,publication_complete=True,now_ns=now)
            assert e.hold_requested
    finally:bridge.close()


def test_candidate_failure_diagnostics_preserve_bounded_causes():
    import json
    from f1_harness.owner_bridge import numerical_failure_detail
    value={'diagnostic':{'code':'registered_candidates_exhausted','metrics':{}},
           'candidates':[{'variant':0,'diagnostic':{'code':'bounded_IK_did_not_reach_goal',
           'metrics':{'position_error_m':.02,'untrusted':'private'}}},
           {'variant':1,'diagnostic':{'code':'scene_sweep_blocked','metrics':{}}}]}
    result=numerical_failure_detail(RuntimeError('registered_candidates_exhausted:'+json.dumps(value)))
    assert [r['code'] for r in result['candidates']]==['bounded_IK_did_not_reach_goal','scene_sweep_blocked']
    assert result['candidates'][0]['metrics']=={'position_error_m':.02}
    assert 'private' not in json.dumps(result)


def test_cartesian_convergence_reason_survives_owner_and_model_projection():
    import json
    from f1_harness.owner_bridge import numerical_failure_detail
    from f1_harness.adapters import _planning_feedback
    raw={'termination':'cartesian_no_progress','cartesian_stalled_ticks':25,
         'position_error_m':.00152,'private':'must_not_escape'}
    detail=numerical_failure_detail(RuntimeError('bounded_IK_did_not_reach_goal:'+json.dumps(raw)))
    feedback=_planning_feedback(detail)
    assert feedback['metrics']['termination']=='cartesian_no_progress'
    assert feedback['metrics']['cartesian_stalled_ticks']==25
    assert 'must_not_escape' not in json.dumps(feedback)


def test_scene_failure_diagnostics_keep_false_distinct_from_unknown():
    import json
    from f1_harness.owner_bridge import numerical_failure_detail
    value={'diagnostic':{'code':'revalidation_object_or_hole_changed','metrics':{
        'skill':'transfer_above_hole','changed_fields':{
            'held':{'before':False,'after':None},'private':'discard'},'private':'discard'}}}
    result=numerical_failure_detail(RuntimeError('revalidation_object_or_hole_changed:'+json.dumps(value)))
    assert result['metrics']['changed_fields']=={'held':{'before':False,'after':None}}
    assert 'private' not in json.dumps(result)


def test_transport_rejection_keeps_route_and_obstacle_through_rpc_wrapping():
    import json
    from f1_harness.owner_bridge import numerical_failure_detail
    metrics = dict(route_family='demo_transport', segment_index=2,
                   obstacle_index=3, obstacle_name='wall_x_low', obstacle_role='bucket',
                   collision_body='tool', variant=0, unrelated='discard')
    detail = numerical_failure_detail(RuntimeError('RPC plan_skill failed: scene_sweep_blocked:' +
        json.dumps(dict(diagnostic=dict(code='scene_sweep_blocked', metrics=metrics)))))
    wrapped = dict(diagnostic=dict(code='registered_candidates_exhausted', metrics={}),
                   candidates=[dict(variant=0, diagnostic=detail)])
    result = numerical_failure_detail(RuntimeError('registered_candidates_exhausted:' + json.dumps(wrapped)))
    kept = result['candidates'][0]['metrics']
    assert kept == {key: value for key, value in metrics.items() if key != 'unrelated'}
    assert 'discard' not in json.dumps(result)


def test_regrasp_sweep_reason_survives_rpc_error_wrapper():
    from f1_harness.owner_bridge import numerical_failure_detail
    result=numerical_failure_detail(RuntimeError('RPC plan_skill failed: regrasp_approach_sweep_blocked'))
    assert result['code']=='regrasp_approach_sweep_blocked'


def test_registered_geometric_rejections_keep_their_codes_for_candidate_fallback():
    from f1_harness.owner_bridge import numerical_failure_detail
    for code in ('grasp_has_no_placement_route','grasp_has_no_transport_route',
                 'grasp_lift_sweep_blocked','pregrasp_target_outside_wrist_view'):
        result=numerical_failure_detail(RuntimeError('RPC plan_skill failed: '+code))
        assert result['code']==code

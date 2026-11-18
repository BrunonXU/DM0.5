import json
import pytest
from f1_harness.contracts import HarnessError
from f1_harness.owner_bridge import JointProposal, numerical_failure_detail
from f1_harness.adapters import _planning_feedback
from f1_harness.geometry_context import entity_status
from f1_harness.planner_context import visual_failure
from test_grasp_selection_planning import setup
from test_unpublished_plan import make_bridge


def test_partial_scene_identifies_missing_aperture_without_losing_block():
    service,obs,_,_=setup()
    partial=obs.model_copy(update={'objects':obs.objects[:1],'geometry_scope':'coarse_object_verification'})
    value=service.dispatch('geometry_context',dict(observation=partial.model_dump(),full22=[0.]*22,held16=partial.state16))
    assert value['object_pose']==list(obs.objects[0].pose)
    assert value['hole_pose'] is None and value['obstacles'] is None
    assert value['missing_entities']==[dict(role='aperture',object_id='hole',reason='not_observed',
        recovery='relocalize_fixture_and_aperture')]
    assert not value['fixture_collision_geometry_available'] and not value['motion_authorized']
    with pytest.raises(HarnessError) as caught:service._scene(partial,[0.]*22,partial.state16)
    detail=_planning_feedback(numerical_failure_detail(caught.value))
    assert detail['code']=='missing_geometry_entities'
    assert detail['metrics']['missing_entities'][0]['object_id']=='hole'
    assert visual_failure({'primitive_calls':[dict(tool='inspect_plan',ok=True,planned=False,
        planning_diagnostic=detail)]})['reason']=='missing_geometry_entities'


def test_partial_pose_frame_mismatch_is_not_downgraded_to_missing_entity():
    service,obs,_,_=setup()
    partial=obs.model_copy(update={'objects':(obs.objects[0].model_copy(update={'frame_id':'wrong'}),)})
    with pytest.raises(HarnessError,match='native_frame_chain_not_registered'):
        service.dispatch('geometry_context',dict(observation=partial.model_dump(),full22=[0.]*22,held16=partial.state16))


def test_owner_combines_geometry_and_preview_on_exact_same_snapshot_despite_new_ingest(tmp_path):
    engine,bridge,clock,state,_,obs=make_bridge(tmp_path)
    intent=engine.pending;engine.pending=None
    captured=[];feedback_calls=[]
    def feedback():
        feedback_calls.append(True)
        return dict(full22=state['full22'],measured16=state['measured16'],stamps16=(clock[0],)*16)
    bridge.measured_feedback=feedback
    def geometry(o,full,held):
        captured.append(('geometry',o,full,held))
        engine.observation=obs.model_copy(update={'observation_id':'later','scene_version':'later'})
        return dict(observation_id=o.observation_id,geometry_status='complete',missing_entities=[])
    bridge.geometry_provider=geometry
    def planner(i,o,full,held):
        captured.append(('preview',o,full,held))
        return JointProposal(i.request_id,i.epoch,i.scene_version,i.calibration_id,tuple(full),
            (tuple(held),),.1,100000000,.5)
    bridge.preview_planner=planner
    try:
        result=bridge.preview_skill(intent.model_dump(),include_geometry=True)
        assert result['geometry']['observation_id']==result['preview']['observation_id']==obs.observation_id
        assert captured[0][1] is captured[1][1] is obs
        assert captured[0][2] is captured[1][2]
        assert len(feedback_calls)==1 and result['planned'] and not result['motion_authorized']
        assert engine.pending is None and engine.running is None
        # Exercise the actual owner envelope through PyRUA, including the
        # assignment-only path used by initial_plan_context (no stdout rescue).
        from pyrualean.arms import ArmToolkit
        from f1_harness.adapters import F1Robot
        class Backend:
            def execute_tool(self,name,args):
                assert name=='inspect_plan'
                return result
            def solved(self):return False
            def env_steps(self):return 0
        arm=ArmToolkit(F1Robot(Backend()),arm='cells',workspace=tmp_path/'worker',images=False)
        reply=arm.execute_tool('python',dict(code='initial_plan = robo.inspect_plan()')).result
        assert not reply.get('exception') and not reply['stdout']
        ledger=reply['primitive_calls'][0]
        assert ledger['placement_advisory']==result['preview']['placement_advisory']
        assert ledger['stages']==result['preview']['stages']
        assert ledger['no_motion'] and not ledger['executable']
    finally:bridge.close()


def test_partial_scene_returns_diagnostics_without_calling_motion_planner(tmp_path):
    engine,bridge,clock,state,_,obs=make_bridge(tmp_path)
    intent=engine.pending;engine.pending=None
    engine.observation=obs.model_copy(update={'geometry_scope':'coarse_object_verification'})
    bridge.measured_feedback=lambda:dict(full22=state['full22'],measured16=state['measured16'],stamps16=(clock[0],)*16)
    missing=entity_status(engine.task,engine.observation)
    bridge.geometry_provider=lambda *a:dict(observation_id=obs.observation_id,**missing)
    def forbidden(*a):raise AssertionError('partial scene sent to motion planner')
    bridge.preview_planner=forbidden
    try:
        result=bridge.preview_skill(intent.model_dump(),include_geometry=True)
        assert not result['planned'] and result['missing_entities']==missing['missing_entities']
        assert result['preview']['planning_diagnostic']['code']=='full_scene_required_for_motion'
        engine.authorized=False
        with pytest.raises(HarnessError):bridge.preview_skill(intent.model_dump(),include_geometry=True)
    finally:bridge.close()

from types import SimpleNamespace
import pytest
from f1_harness.adapters import F1Robot
from f1_harness.planner_context import visual_failure,gather_images
from f1_harness.planner_context import initial_plan_context,visual_failure_key


@pytest.mark.parametrize('reason', ['stale_observation_measurements','scene_expired'])
def test_only_stale_readonly_preview_reacquires_once(reason):
    robot=object.__new__(F1Robot);calls=[]
    def read(tool,args):
        calls.append(tool)
        if tool=='preview_skill' and calls.count(tool)==1:raise RuntimeError(reason)
        return {'planned':True}
    robot._readonly=read
    assert robot.preview('approach_grasp')['planned']
    assert calls==['preview_skill','observe','preview_skill']


@pytest.mark.parametrize('reason', ['feedback_expired:right','authority_ended','connection_lost','unknown_publication'])
def test_unrelated_failure_is_not_retried(reason):
    robot=object.__new__(F1Robot);calls=[]
    def read(tool,args):calls.append(tool);raise RuntimeError(reason)
    robot._readonly=read
    with pytest.raises(RuntimeError):robot.preview('approach_grasp')
    assert calls==['preview_skill']
    assert visual_failure(dict(exception=reason+' insufficient valid depth')) is None


def test_evidence_is_two_readonly_views_and_preserves_failed_checkpoint():
    failed={'primitive_calls':[dict(tool='execute_skill',request_id='original',outcome='pending_review',reason='insufficient valid depth')]}
    d=visual_failure(failed);assert d['failed_calls'][0]['request_id']=='original'
    class Arm:
        def execute_tool(self,name,args):
            assert name=='python' and 'execute_skill' not in args['code'] and 'motion(' not in args['code']
            assert '"right", "head"' in args['code']
            return SimpleNamespace(result={'moved':False},images=[b'right',b'head'])
    class Store:
        def event(self,*a,**kw):pass
    result,receipt=gather_images(Arm(),Store(),diagnostic=d)
    assert len(result.images)==2 and receipt['motion_requested'] is False


def test_actual_pyrua_context_is_readonly_and_combines_preview(tmp_path):
    from pyrualean.arms import ArmToolkit
    calls=[]
    class Backend:
        def execute_tool(self,name,args):
            calls.append(name)
            if name=='inspect_plan':return dict(geometry=dict(world_frame='world',observation_id='fresh'),
                preview=dict(planned=True,no_motion=True,observation_id='fresh'),planned=True,current_scope=True)
            raise AssertionError(name)
        def solved(self):return False
        def env_steps(self):return 0
    arm=ArmToolkit(F1Robot(Backend()),arm='cells',workspace=tmp_path,images=False)
    result=arm.execute_tool('python',dict(code='print(robo.inspect_plan())')).result
    assert not result.get('exception') and not result['moved']
    assert calls==['inspect_plan']
    assert result['primitive_calls'][0]['planned'] is True


@pytest.mark.parametrize('planned',[True,False])
def test_initial_context_keeps_complete_geometry_in_ledger_not_truncated_stdout(tmp_path,planned):
    from pyrualean.arms import ArmToolkit
    calls=[]
    class Backend:
        def execute_tool(self,name,args):
            calls.append(name)
            return dict(planned=planned,current_scope=True,fixture_transit_available=True,
                aperture_required_for=['align','insert'],missing_entities=[dict(role='aperture',object_id='hole')],
                geometry=dict(current_tcp_pose=[1.]*16,obstacles=[dict(min=[0.]*3,max=[1.]*3)]*100),
                planning_diagnostic=None if planned else dict(code='missing_geometry_entities'))
        def solved(self):return False
        def env_steps(self):return 0
    arm=ArmToolkit(F1Robot(Backend()),arm='cells',workspace=tmp_path,images=False)
    feedback=initial_plan_context(arm)
    assert not feedback.get('exception') and calls==['inspect_plan']
    assert not feedback['stdout']
    receipt=feedback['primitive_calls'][0]
    assert receipt['planned'] is planned and receipt['fixture_transit_available']
    assert receipt['missing_entities'][0]['object_id']=='hole'
    if not planned:
        assert len(receipt['geometry']['obstacles'])==100
        assert receipt['geometry']['current_tcp_pose']==[1.]*16
    else:
        assert 'geometry' not in receipt
    assert bool(visual_failure(feedback)) is not planned
    # The model can reuse initial geometry without another RPC or print dump.
    reused=arm.execute_tool('python',dict(code="print(g == initial_plan.get('geometry', {}))")).result
    assert not reused.get('exception') and reused['stdout'].strip()=='True'
    assert calls==['inspect_plan']


def test_automatic_visual_key_ignores_transport_ids_but_preserves_changed_strategy():
    from copy import deepcopy
    first=dict(reason='numerical_plan_rejected',failed_calls=[dict(tool='execute_skill',request_id='old',
        verification_request_id='old',kwargs=dict(name='approach_grasp',parameters=dict(pitch_offset_deg=10)),
        outcome='pending_review',planning_diagnostic=dict(code='scene_sweep_blocked',metrics=dict(
            obstacle_name='bucket',observation_id='old',segment_index=2)))])
    second=deepcopy(first);row=second['failed_calls'][0]
    row.update(request_id='new',verification_request_id='new')
    row['planning_diagnostic']['metrics']['observation_id']='new'
    assert visual_failure_key(first)==visual_failure_key(second)
    row['kwargs']['parameters']['pitch_offset_deg']=20
    assert visual_failure_key(first)!=visual_failure_key(second)
    assert visual_failure_key(first)!=visual_failure_key(first,['approach_grasp'])


def test_failure_does_not_automatically_send_images(tmp_path):
    from f1_harness.planner import ResponsesPlanner
    from f1_harness.evidence import EvidenceStore
    class Arm:
        def get_tools_spec(self):return [dict(name=n,description=n,input_schema={}) for n in ['python','finish']]
        def execute_tool(self,name,args):
            if name=='finish':return SimpleNamespace(result={'status':'stuck'},images=[])
            if args['code']=='fixture_failed':return SimpleNamespace(result={'primitive_calls':[dict(tool='execute_skill',request_id='failed',outcome='pending_review',reason='insufficient valid depth')]},images=[])
            assert 'robo.show' in args['code'] and 'motion(' not in args['code']
            return SimpleNamespace(result={'moved':False,'stdout':'evidence_camera right\nevidence_camera head'},images=[b'\x89PNGfixture1',b'\x89PNGfixture2'])
    class Planner(ResponsesPlanner):
        def __init__(self):self.store=EvidenceStore(tmp_path);self.turn=0
        def request(self,**kw):
            self.turn+=1;self.last_request_id=str(self.turn)*32
            if self.turn==1:return {'output':[dict(type='function_call',name='python',call_id='first',arguments='{"code":"fixture_failed"}')]}
            images=[c for m in kw['input'] if m.get('role')=='user' and isinstance(m.get('content'),list) for c in m['content'] if c['type']=='input_image']
            assert len(images)==0
            return {'output':[dict(type='function_call',name='finish',call_id='last',arguments='{}')]}
    planner=Planner();assert planner.run(Arm(),task_text='fixture')['status']=='stuck';assert planner.turn==2

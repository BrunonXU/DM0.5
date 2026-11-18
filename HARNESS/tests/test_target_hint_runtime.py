"""Perception hints cannot fabricate geometry or cross motion/owner epochs."""
from types import SimpleNamespace
import threading
import numpy as np
import pytest

from f1_harness.live_scene import LiveSceneObserver
from f1_harness.target_relocalization import source_search_box
from f1_harness.evidence import EvidenceStore
from f1_harness.adapters import F1Robot, F1Toolkit, ToolkitBackend
from pyrualean.arms import ArmToolkit
from test_unpublished_plan import make_bridge


def setup(tmp_path):
    engine,bridge,clock,state,poll,obs=make_bridge(tmp_path/'engine')
    bridge.close()
    context=dict(session_id='s',owner_instance_id='owner',task_hash='task',epoch=1,phase='observe')
    status=dict(authorized=True,hold_requested=False,running=None,pending=None,perception_context=context)
    live=LiveSceneObserver.__new__(LiveSceneObserver)
    live.owner=SimpleNamespace(call=lambda method:status)
    live.raw=SimpleNamespace(observe=lambda:obs,store=SimpleNamespace(blob=lambda key:b'pixels'))
    live.store=EvidenceStore(tmp_path/'scene');live.lock=threading.Lock()
    live.image_issuances={};live.pending_target_hints={}
    live.config={'geometry':dict(camera='right',object_id='block',roi=[0,0,2,2],k_rgb=np.eye(3).tolist(),d_rgb=[])}
    updates=[]
    live.pipeline=SimpleNamespace(segment=SimpleNamespace(set_search_hint=lambda **kw:updates.append(kw)),
        invalidate=lambda:None,invalidate_secondary=lambda:None)
    issued=live.camera_observation()
    meta=dict(frame=issued['frames'][0],observation_id=issued['observation_id'],roi=None,
        source_image_size=[2,2],model_image_size=[2,2])
    hint=source_search_box(meta,[.1,.1,.9,.9])
    args=dict(session_id='s',owner_instance_id='owner',task_hash='task',epoch=1,hint=hint)
    return live,obs,status,context,updates,args


def test_hint_requires_new_frame_and_never_ingests_scene(tmp_path):
    live,obs,status,context,updates,args=setup(tmp_path)
    receipt=live.locate_target(**args)
    assert receipt['accepted'] and not receipt['motion_evidence']
    live._apply_target_hint(obs,context);assert not updates
    fresh=obs.model_copy(update=dict(frames=tuple(f.model_copy(update=dict(
        received_ns=f.received_ns+1,source_ns=f.source_ns+1)) for f in obs.frames)))
    live._apply_target_hint(fresh,context)
    assert len(updates)==1 and updates[0]['object_id']=='block'
    assert not live.pending_target_hints


def two_view_hints(tmp_path):
    live,obs,status,context,updates,args=setup(tmp_path)
    head_frames=tuple(f.model_copy(update={'camera':'head','frame_id':'head_'+f.stream}) for f in obs.frames)
    obs=obs.model_copy(update={'frames':obs.frames+head_frames})
    live.raw.observe=lambda:obs
    live.config['geometry']['hole_camera']=dict(live.config['geometry'],camera='head')
    live.pipeline.segment.registered_entities=lambda camera:('block','hole','anchor')
    issued=live.camera_observation()
    hints={}
    for camera in ('head','right'):
        frame=next(f for f in issued['frames'] if f['camera']==camera)
        meta=dict(frame=frame,observation_id=issued['observation_id'],roi=None,
            source_image_size=[2,2],model_image_size=[2,2])
        hint=source_search_box(meta,[.1,.1,.9,.9])
        if camera=='head':hint['entity_hints']=[dict(hint,object_id='hole')]
        hints[camera]=dict(args,hint=hint)
    return live,obs,context,updates,hints


def test_two_camera_hints_in_one_model_turn_are_both_applied(tmp_path):
    live,obs,context,updates,hints=two_view_hints(tmp_path)
    for args in hints.values():live.locate_target(**args)
    assert set(live.pending_target_hints)=={'head','right'}
    fresh=obs.model_copy(update={'frames':tuple(f.model_copy(update={
        'received_ns':f.received_ns+1,'source_ns':f.source_ns+1}) for f in obs.frames)})
    live._apply_target_hint(fresh,context)
    assert {(v['view'],v['object_id']) for v in updates}=={('head','hole'),('right','block')}
    assert not live.pending_target_hints


def test_stale_head_does_not_discard_pending_hint_when_right_advances(tmp_path):
    live,obs,context,updates,hints=two_view_hints(tmp_path)
    for args in hints.values():live.locate_target(**args)
    fresh=obs.model_copy(update={'frames':tuple(f.model_copy(update={
        'received_ns':f.received_ns+1,'source_ns':f.source_ns+1}) if f.camera=='right' else f for f in obs.frames)})
    live._apply_target_hint(fresh,context)
    assert [(v['view'],v['object_id']) for v in updates]==[('right','block')]
    assert set(live.pending_target_hints)=={'head'}
    live._apply_target_hint(fresh,dict(context,epoch=2))
    assert not live.pending_target_hints and len(updates)==1


def test_search_hint_invalidates_public_scene_before_new_measurement(tmp_path):
    live,obs,status,context,updates,args=setup(tmp_path)
    live.latest=obs;live.error=None
    live.locate_target(**args)
    assert live.latest is None and live.target_hint_generation==1
    with pytest.raises(ValueError,match='target_search_hint_pending'):
        live.observe()
    live._apply_target_hint(obs,context)
    assert not updates and live.latest is None


def test_new_hint_while_processing_rejects_prior_prompt_result(monkeypatch):
    from test_phase_live_scene import observer
    from f1_harness.live_scene import SceneNotAdvanced
    live,verified=observer(monkeypatch)
    def process(value,**kwargs):
        # The request thread queues a new hint during the previous pixel work.
        with live.lock:
            live.target_hint_generation=1
            live.latest=None
            live.error='scene_not_ready:target_search_hint_pending'
        return value
    live.pipeline.process=process
    with pytest.raises(SceneNotAdvanced):live._sample()
    assert not verified and not live.ingested and live.latest is None


@pytest.mark.parametrize('change',['authority','running','epoch','source'])
def test_invalid_hint_cannot_mutate_prompt(tmp_path,change):
    live,obs,status,context,updates,args=setup(tmp_path)
    if change=='authority':status['authorized']=False
    if change=='running':status['running']={'request_id':'moving'}
    if change=='epoch':args['epoch']=2
    if change=='source':args['hint']['frame']['blob_sha256']='2'*64
    with pytest.raises(ValueError):live.locate_target(**args)
    assert not live.pending_target_hints and not updates


def test_queued_hint_does_not_cross_acquisition_restart(tmp_path):
    live,obs,status,context,updates,args=setup(tmp_path)
    live.locate_target(**args)
    live._apply_target_hint(obs.model_copy(update=dict(acquisition_instance='restarted')),context)
    assert not live.pending_target_hints and not updates


def test_passive_pyrua_summary_does_not_call_observe(tmp_path):
    class Backend:
        def execute_tool(self,*args):raise AssertionError('unexpected hidden geometry read')
        def solved(self):return False
        def env_steps(self):return 0
    arm=ArmToolkit(F1Robot(Backend()),arm='cells',workspace=tmp_path,images=False)
    result=arm.execute_tool('python',{'code':'def helper():\n    return 1'}).result
    assert not result.get('error') and not result.get('exception')


def test_real_pyrua_and_rpent_hint_receipt_does_not_capture_missing_geometry(tmp_path):
    class Backend:
        def __init__(self):self.calls=[]
        def execute_tool(self,name,args):
            self.calls.append(name)
            if name=='locate_target':
                return dict(accepted=True,applied=False,search_only=True,motion_evidence=False)
            raise AssertionError('hint must not implicitly request geometry')
        def solved(self):return False
        def env_steps(self):return 0
    backend=Backend()
    robot=F1Robot(ToolkitBackend(F1Toolkit(backend,tmp_path/'memory')))
    arm=ArmToolkit(robot,arm='cells',workspace=tmp_path/'cell',images=False)
    result=arm.execute_tool('python',{'code':"hint = robo.locate_object('head', [0.1, 0.2, 0.3, 0.4])\nassert hint['accepted']\nassert not hint['motion_evidence']"}).result
    assert not result.get('error') and not result.get('exception')
    assert backend.calls==['locate_target']
    assert robot._last_state is None
    record=robot._ledger.records[-1]
    assert record.stateful and record.ok and record.summary['accepted']


def test_rpent_skill_still_captures_measured_state(tmp_path):
    class Backend:
        def __init__(self):self.calls=[]
        def execute_tool(self,name,args):
            self.calls.append(name)
            if name=='execute_skill':return dict(outcome='success',request_id='request-1')
            if name=='observe':
                return dict(observation=dict(observation_id='measured-1',objects=[]),
                            status=dict(authorized=True,phase='grasp'))
            raise AssertionError(name)
        def solved(self):return False
    backend=Backend();toolkit=F1Toolkit(backend,tmp_path/'memory')
    result=toolkit.execute_tool('execute_skill',dict(name='grasp')).result
    assert backend.calls==['execute_skill','observe']
    assert result['state']['observation_id']=='measured-1'
    assert result['log']['result']['outcome']=='success'


def test_fixture_only_batch_does_not_clear_grasp_following(tmp_path):
    live,obs,status,context,updates,args=setup(tmp_path)
    live.config['geometry'].update(hole_id='hole',fixture_occlusion={'anchors':{'anchor':{}}})
    live.pipeline.segment.registered_entities=lambda camera:('block','hole','anchor')
    invalidations=[]
    live.pipeline.reject_measurement=lambda reason:invalidations.append(reason)
    live.pipeline.invalidate_secondary=lambda:invalidations.append('fixture')
    live.pipeline.search_hint='held-search'
    args['hint']['entity_hints']=[dict(args['hint'],object_id=key) for key in ('hole','anchor')]
    live.locate_target(**args)
    fresh=obs.model_copy(update=dict(frames=tuple(f.model_copy(update=dict(
        received_ns=f.received_ns+1,source_ns=f.source_ns+1)) for f in obs.frames)))
    live._apply_target_hint(fresh,context)
    assert [v['object_id'] for v in updates]==['hole','anchor']
    assert invalidations==['fixture'] and live.pipeline.search_hint=='held-search'
    catalog=live.search_entities()
    assert catalog['right']==[dict(object_id='block',role='target_block'),
        dict(object_id='hole',role='target_aperture'),dict(object_id='anchor',role='fixture_anchor')]


@pytest.mark.parametrize('fault',['duplicate','frame','unknown'])
def test_invalid_entity_batch_does_not_queue_partial_update(tmp_path,fault):
    live,obs,status,context,updates,args=setup(tmp_path)
    live.pipeline.segment.registered_entities=lambda camera:('block','hole')
    values=[dict(args['hint'],object_id='block'),dict(args['hint'],object_id='hole')]
    if fault=='duplicate':values[1]['object_id']='block'
    if fault=='frame':values[1]['frame']=dict(values[1]['frame'],blob_sha256='f'*64)
    if fault=='unknown':values[1]['object_id']='made-up'
    args['hint']['entity_hints']=values
    with pytest.raises(ValueError):live.locate_target(**args)
    assert not live.pending_target_hints and not updates


def test_batch_pyrua_tool_has_no_hidden_observation(tmp_path):
    class Backend:
        def __init__(self):self.calls=[]
        def execute_tool(self,name,args):
            self.calls.append(name)
            if name=='locate_entities':return dict(accepted=True,motion_evidence=False)
            raise AssertionError('batch hint unexpectedly queried geometry')
        def solved(self):return False
        def env_steps(self):return 0
    backend=Backend();robot=F1Robot(ToolkitBackend(F1Toolkit(backend,tmp_path/'memory')))
    arm=ArmToolkit(robot,arm='cells',workspace=tmp_path/'cells',images=False)
    result=arm.execute_tool('python',{'code':"robo.locate_entities('head', [{'object_id':'hole', 'box_normalized':[.1,.2,.3,.4]}])"}).result
    assert not result.get('error') and not result.get('exception')
    assert backend.calls==['locate_entities']


def test_hint_seeds_issued_pixels_and_original_source_before_current_frame(tmp_path,monkeypatch):
    live,obs,status,context,updates,args=setup(tmp_path)
    import f1_harness.images as images
    pixels=np.full((2,2,3),73,np.uint8)
    monkeypatch.setattr(images,'decode',lambda frame,data:pixels.copy())
    monkeypatch.setattr(live.store,'blob',lambda sha:b'pixels' if sha==args['hint']['frame']['blob_sha256'] else None)
    seeded=[]
    live.pipeline.segment.seed_issued_image=lambda **kw:seeded.append(kw) or True
    live.locate_target(**args)
    fresh=obs.model_copy(update=dict(frames=tuple(f.model_copy(update=dict(
        received_ns=f.received_ns+100,source_ns=f.source_ns+100)) for f in obs.frames)))
    live._apply_target_hint(fresh,context)
    assert len(seeded)==1 and len(updates)==1
    assert np.array_equal(seeded[0]['rgb'],pixels)
    assert seeded[0]['source']['source_ns']==args['hint']['frame']['source_ns']
    assert seeded[0]['source']['source_ns']!=fresh.frames[0].source_ns
    assert live.latest is None

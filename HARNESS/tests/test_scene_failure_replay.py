"""Saved diagnostic bytes -> actual segmentation/geometry, never motion evidence."""
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from f1_harness.evidence import EvidenceStore, FrameCache
from f1_harness.scene_failures import SceneFailureRecorder
from test_scene_pipeline import fixture

spec=importlib.util.spec_from_file_location('failure_replay',Path(__file__).parents[1]/'scripts/replay_scene_failure.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


@pytest.fixture
def captured(tmp_path):
    pipeline,obs=fixture();cfg=pipeline.config if hasattr(pipeline,'config') else pipeline.cfg
    cfg=dict(cfg)
    cache=FrameCache();frames=[]
    rgb=np.zeros((20,20,3),np.uint8)
    rgb[8:13,8:13]=[255,0,0]
    rgb[6:15,6:15]=[0,255,0];rgb[7:14,7:14]=0;rgb[8:13,8:13]=[255,0,0]
    for frame in obs.frames:
        info=cache.put_blob(json.dumps({'K':cfg['k_rgb'],'stream':frame.stream}).encode())
        cfg[frame.stream+'_info_sha256']=info
        data=rgb.tobytes() if frame.stream=='rgb' else np.full((20,20),1000,np.uint16).tobytes()
        sha=cache.put_blob(data)
        frames.append(frame.model_copy(update=dict(blob_sha256=sha,camera_info_sha256=info,
            encoding='rgb8' if frame.stream=='rgb' else frame.encoding)))
    obs=obs.model_copy(update={'frames':tuple(frames)})
    config=dict(backend='fixed_color',prompts={'block':[1,1,5,5],'hole':[4,4,17,17]},
        image_shape=[20,20,3],minimum_score=.9,minimum_area_px=4,prompt_padding_px=1,
        maximum_overlap_fraction=.1,color_profiles=dict(
            block=dict(hsv_lower=[0,200,200],hsv_upper=[5,255,255],minimum_area_px=4,maximum_area_px=100),
            hole=dict(hsv_lower=[55,200,200],hsv_upper=[65,255,255],minimum_area_px=4,maximum_area_px=100)))
    snapshot=dict(context=dict(owner_instance_id='test-owner',session_id='test-session',task_hash='test-hash',
        task_id='web-'+'a'*32,authorized=True,phase='approach_grasp'),
        observation=obs.model_dump(),geometry_config=cfg,base_eef=np.eye(4).tolist(),
        feedback=dict(measured16=[0.]*16,stamps16=[1]*16),
        search_before=dict(prompts={'default':dict(backend='fixed_color',
            prompts={'block':[6,6,15,15],'hole':[4,4,17,17]})}))
    store=EvidenceStore(tmp_path/'store');recorder=SceneFailureRecorder(store)
    assert recorder.offer('test-recorded-failure',snapshot,cache.blob)
    assert not recorder.close()['writer_alive']
    path=next(recorder.root.glob('*.json'))
    config_path=tmp_path/'mask.json';config_path.write_text(json.dumps(config))
    return path,config_path


def mutate(path,fn):
    data=json.loads(path.read_text());fn(data);path.write_text(json.dumps(data))


def test_recorded_bytes_restore_search_and_compute_only_single_frame(captured):
    path,config=captured
    report=module.replay(path,config)
    assert report['status']=='replayed_with_incomplete_temporal_state'
    assert report['views']['head']==dict(status='masks_observed',pixels={'block':25,'hole':32})
    assert report['geometry']['status']=='single_frame_computed'
    assert report['prompts_restored'] and not report['mask_config_identity_verified']
    assert report['input_timestamps']['observation_received_ns']==1
    assert len(report['verified_blob_sha256'])==4
    assert not report['motion_admission'] and not report['experience_promotion_allowed']
    assert not report['robot_connected'] and report['paid_model_calls']==0
    assert all('held' not in obj and 'inserted' not in obj for obj in report['geometry']['objects'])
    json.dumps(report,allow_nan=False)
    # Without the saved search, the config's original box misses this object.
    mutate(path,lambda d:d['search_before'].pop('prompts'))
    missed=module.replay(path,config)
    assert missed['views']['head']['status']=='rejected'
    assert 'mask_uncertain' in missed['views']['head']['reason']


def test_new_snapshot_binds_mask_configuration_without_claiming_temporal_replay(captured):
    from f1_harness.contracts import digest
    path,config=captured
    mutate(path,lambda d:d.update(mask_config_sha256=digest(json.loads(config.read_text()))))
    report=module.replay(path,config)
    assert report['mask_config_identity_verified']
    assert report['status']=='replayed_with_incomplete_temporal_state'
    assert not report['motion_admission'] and not report['experience_promotion_allowed']
    mutate(config,lambda d:d.update(minimum_score=.8))
    with pytest.raises(ValueError,match='mask_configuration_identity_changed'):module.replay(path,config)


def test_missing_depth_is_reported_not_reconstructed(captured):
    path,config=captured;record=json.loads(path.read_text())
    sha=next(f['blob_sha256'] for f in record['observation']['frames'] if f['stream']=='depth')
    (path.parent/'blobs'/sha).unlink()
    result=module.replay(path,config)
    assert result['status']=='input_incomplete' and result['missing_blobs']==[sha]
    assert result['geometry'] is None and result['views']=={}


def test_changed_bytes_fail_integrity_before_geometry(captured):
    path,config=captured;blob=next((path.parent/'blobs').iterdir());blob.write_bytes(b'changed')
    with pytest.raises(ValueError,match='blob_hash_mismatch'):module.replay(path,config)


@pytest.mark.parametrize('field',['session_id','owner_instance_id','task_id','task_hash'])
def test_missing_task_identity_rejected(captured,field):
    path,config=captured;mutate(path,lambda d:d['context'].pop(field))
    with pytest.raises(ValueError,match='task_identity_missing'):module.replay(path,config)


def test_model_backend_refused_before_provider_construction(captured,monkeypatch):
    path,config=captured;mutate(config,lambda d:d.update(backend='sam2'))
    monkeypatch.setattr(module,'ConfiguredMaskProvider',lambda *a,**k:pytest.fail('must not load model'))
    with pytest.raises(ValueError,match='requires_local_fixed_color'):module.replay(path,config)


@pytest.mark.parametrize('bad',['view','object','box'])
def test_saved_prompt_identity_and_shape_cannot_be_silently_substituted(captured,bad):
    path,config=captured
    def change(d):
        prompts=d['search_before']['prompts']
        if bad=='view':prompts['wrong']=prompts.pop('default')
        elif bad=='object':prompts['default']['prompts']['wrong']=prompts['default']['prompts'].pop('block')
        else:prompts['default']['prompts']['block']=[6,6,5,5]
    mutate(path,change)
    with pytest.raises(ValueError,match='saved_mask_'):module.replay(path,config)


def test_missing_feedback_and_invalid_depth_scale_remain_specific(captured):
    path,config=captured
    mutate(path,lambda d:d.update(feedback=None))
    assert module.replay(path,config)['geometry']['reason']=='matched_feedback_or_eef_missing'
    def change(d):d['observation']['frames'][1]['depth_scale_m']=1.
    mutate(path,change)
    assert module.replay(path,config)['views']['head']['reason']=='secondary_camera_identity_changed'


def test_symlink_and_filename_mismatch_rejected(captured,tmp_path):
    path,config=captured;link=tmp_path/path.name;link.symlink_to(path)
    with pytest.raises(ValueError,match='not_regular'):module.replay(link,config)
    mutate(path,lambda d:d.update(snapshot_id='wrong'))
    with pytest.raises(ValueError,match='filename_identity_mismatch'):module.replay(path,config)


def test_cli_writes_report_but_never_overwrites(captured,tmp_path,monkeypatch):
    path,config=captured;output=tmp_path/'report.json'
    monkeypatch.setattr('sys.argv',['replay','--snapshot',str(path),'--mask-config',str(config),'--output',str(output)])
    module.main();original=output.read_bytes()
    with pytest.raises(FileExistsError):module.main()
    assert output.read_bytes()==original


def test_real_seal_export_extract_and_replay_round_trip(captured,tmp_path):
    import shutil
    from f1_harness.collect import REMOTE_READER
    from test_scene_failure_export import export
    path,config=captured;record=json.loads(path.read_text());context=record['context']
    root=tmp_path/'runs';gateway=root/('services-'+context['task_id'])/'processes/gateway'
    shutil.copytree(path.parent,gateway/'scene-failures')
    owner=tmp_path/'owner';(owner/'harness').mkdir(parents=True)
    (owner/'harness/trace.jsonl').write_text(json.dumps(dict(kind='task_prepared',
        owner_instance_id=context['owner_instance_id'],session_id=context['session_id'],task_hash=context['task_hash']))+'\n')
    lock=tmp_path/'control.lock';lock.touch()
    request={key:context[key] for key in ('owner_instance_id','session_id','task_hash','task_id')}
    request.update(owner_run_path=str(owner),control_lock=str(lock),max_bytes=1024*1024)
    reader=REMOTE_READER.replace("Path('/home/user/Bruno/HARNESS/runs')",'Path('+repr(str(root))+')')
    manifest=export(tmp_path,request,reader)
    row=manifest['scene_manifest']['diagnostics']['snapshots'][0]
    assert row['export_complete'] and row['capture_complete']
    archived=tmp_path/'exported/owner'/row['path']
    before=module.replay(path,config);after=module.replay(archived,config)
    assert before['geometry']==after['geometry'] and before['views']==after['views']
    assert before['verified_blob_sha256']==after['verified_blob_sha256']
    assert after['matched_feedback']==record['feedback']


def test_invalid_feedback_mask_never_becomes_valid_geometry(captured):
    path,config=captured
    def change(d):d['observation']['state_valid_mask'][7]=False
    mutate(path,change)
    result=module.replay(path,config)
    assert result['views']['head']['status']=='masks_observed'
    assert result['geometry']==dict(status='rejected',reason='measured_right_arm_missing')


def test_missing_search_history_is_reported_and_not_used_as_new_measurement(captured):
    path,config=captured
    mutate(path,lambda d:d['search_before'].update(hint={'time_ns':0,'pose':[999,999,999]}))
    result=module.replay(path,config)
    assert result['recorded_search_hint']['pose']==[999,999,999]
    assert result['geometry']['status']=='single_frame_computed'
    assert result['status']=='replayed_with_incomplete_temporal_state'

import numpy as np
import pytest

from f1_harness.partial_scene import observed_free_space_view, observed_target_bounds
from test_full_development_scene import full_scene, sample


def test_free_depth_tiles_keep_holes_unknown_and_ray_origin_separate_from_return():
    import base64,hashlib,zlib
    pipeline,original=full_scene();obs=sample(original,0);cfg=pipeline.config
    frame=next(f for f in obs.frames if f.camera=='right' and f.stream=='rgb')
    depth=np.full((frame.height,frame.width),.5);depth[0,0]=0
    view=observed_free_space_view(depth,cfg,np.eye(4),obs.frames)
    assert view.min_depth_m[0]==0 and view.min_depth_m[1]==.5
    assert view.near_depth_m==0.
    assert view.source_rgb_sha256==frame.blob_sha256
    raw=zlib.decompress(base64.b64decode(view.fine_depth_zlib_base64))
    fine=np.frombuffer(raw,dtype='<f4').reshape(depth.shape)
    assert hashlib.sha256(raw).hexdigest()==view.fine_depth_sha256
    assert fine[0,0]==0 and 0<fine[0,1]<depth[0,1]
    assert np.all(fine<=depth)
    pipeline.close()


def test_surface_bounds_do_not_claim_unseen_registered_object_volume():
    depth=np.full((10,10),.5);mask=np.zeros((10,10),bool);mask[1:9,1:9]=True
    view=dict(roi=[0,0,10,10],k_rgb=[[100,0,5],[0,100,5],[0,0,1]])
    bounds=observed_target_bounds(mask,depth,view,np.eye(4),dict(min=[-.015,-.015,0],max=[.015,.015,.06]))
    assert bounds['depth_points']==64 and bounds['mask_pixels']==64
    assert bounds['observed_surface_min'][2]==bounds['observed_surface_max'][2]==.5
    assert bounds['registered_object_diagonal_m']==pytest.approx(np.linalg.norm([.03,.03,.06]))


class ConfirmedMask:
    def __init__(self,original,*,status='confirmed',stale=False):
        self.original=original;self.status=status;self.stale=stale;self.sources={}
    def segment_frame(self,rgb,*,view,source,boxes=None):
        self.sources[view]=source
        return self.original(rgb,view)
    def diagnostic_snapshot(self):
        return {view:{'block':{'tracking':dict(identity_status=self.status,identity_source='semantic',
            source_ns=source['source_ns']-(1 if self.stale else 0))}} for view,source in self.sources.items()}


def partial_fixture(monkeypatch,**mask_options):
    import f1_harness.scene_pipeline as module
    pipeline,obs=full_scene()
    pipeline.config.update(object_reacquisition_camera='head',
        search_object_box=dict(min=[-.015,-.015,0],max=[.015,.015,.06]))
    pipeline.segment=ConfirmedMask(pipeline.segment,**mask_options)
    def failure(**kwargs):
        kwargs['diagnostics']['surface_selection']=dict(input_points=300,failed_checks=['observed_face_is_too_narrow'])
        raise ValueError('observed_face_is_too_narrow')
    monkeypatch.setattr(module,'object_from_mask',failure)
    return pipeline,obs


def test_current_confirmed_depth_survives_missing_pose_without_claiming_held(monkeypatch):
    pipeline,original=partial_fixture(monkeypatch)
    try:
        obs=sample(original,0);value=pipeline.process(obs,base_eef=np.eye(4))
        assert value.geometry_scope=='coarse_local_environment'
        obj=value.objects[0]
        assert obj.visible and obj.pose is None and obj.held is None and obj.stationary is None
        assert value.fixture is None and not value.stationary
        evidence=value.local_collision_evidence
        assert evidence.observation_id==obs.observation_id
        assert evidence.state_received_ns==obs.state_received_ns
        radius=evidence.registered_object_diagonal_m+evidence.planning_margin_m
        np.testing.assert_allclose(np.asarray(evidence.possible_occupancy_min),np.asarray(evidence.observed_surface_min)-radius)
        assert {v.camera for v in evidence.free_space_views}=={'right','head'}
        assert pipeline.last_timing['object_geometry_attempts'][0]['reason']=='observed_face_is_too_narrow'
        assert pipeline.last_timing['partial_object_geometry']['identity_provenance']['source_ns']==1
    finally:pipeline.close()


@pytest.mark.parametrize('options',[dict(status='unresolved'),dict(stale=True)])
def test_unresolved_identity_or_old_source_cannot_become_partial_geometry(monkeypatch,options):
    pipeline,original=partial_fixture(monkeypatch,**options)
    try:
        with pytest.raises(ValueError,match='observed_face_is_too_narrow'):
            pipeline.process(sample(original,0),base_eef=np.eye(4))
        assert 'partial_object_geometry' not in pipeline.last_timing
    finally:pipeline.close()


def test_secondary_identity_fault_cannot_be_hidden_by_partial_primary(monkeypatch):
    pipeline,original=partial_fixture(monkeypatch)
    original_view=pipeline._secondary_view
    def fail(*args,**kwargs):raise ValueError('camera_calibration_identity_changed')
    pipeline._secondary_view=fail
    try:
        with pytest.raises(ValueError,match='camera_calibration_identity_changed'):
            pipeline.process(sample(original,0),base_eef=np.eye(4))
        assert 'partial_object_geometry' not in pipeline.last_timing
    finally:pipeline._secondary_view=original_view;pipeline.close()


def test_partial_object_only_and_full_scopes_keep_current_independent_capabilities(monkeypatch):
    import f1_harness.scene_pipeline as module
    from f1_harness.scene_pipeline import SecondaryCameraUnavailable
    pipeline,original=full_scene()
    pipeline.config.update(object_reacquisition_camera='head',
        search_object_box=dict(min=[-.015,-.015,0],max=[.015,.015,.06]))
    pipeline.segment=ConfirmedMask(pipeline.segment)
    original_measure=module.object_from_mask;mode=['partial']
    def measured(**kwargs):
        if mode[0]=='partial':
            raise ValueError('observed_face_is_too_narrow')
        return original_measure(**kwargs)
    monkeypatch.setattr(module,'object_from_mask',measured)
    try:
        partial=pipeline.process(sample(original,0),base_eef=np.eye(4))
        assert partial.objects[0].pose is None and partial.local_collision_evidence is not None
        mode[0]='measured'
        current=sample(original,1).model_copy(update=dict(local_collision_evidence=partial.local_collision_evidence))
        only=pipeline.process(current,base_eef=np.eye(4),
            secondary_error=SecondaryCameraUnavailable('secondary_camera_pairing_unavailable'))
        assert only.geometry_scope=='coarse_object_verification'
        assert only.objects[0].pose is not None and only.objects[0].held is None
        assert only.local_collision_evidence is not None
        assert only.local_collision_evidence.measured_at_ns==current.received_ns
        assert {f.camera for f in only.frames}=={'right'}
        assert {v.camera for v in only.local_collision_evidence.free_space_views}=={'right'}
        assert not any(w.startswith('object_pose_unavailable') for w in only.quality_warnings)
        assert pipeline.last_timing['partial_object_geometry']['object_pose_available']
        full=pipeline.process(sample(original,2).model_copy(update=dict(
            local_collision_evidence=only.local_collision_evidence)),base_eef=np.eye(4))
        assert full.geometry_scope=='coarse_static_task' and len(full.objects)==2
        assert full.local_collision_evidence is None
        pipeline.segment.stale=True
        stale=pipeline.process(sample(original,3).model_copy(update=dict(
            local_collision_evidence=only.local_collision_evidence)),base_eef=np.eye(4),
            secondary_error=SecondaryCameraUnavailable('secondary_camera_pairing_unavailable'))
        assert stale.objects[0].pose is not None and stale.local_collision_evidence is None
    finally:pipeline.close()


def test_object_only_attached_object_does_not_claim_open_hand_local_environment(monkeypatch):
    from f1_harness.scene_pipeline import SecondaryCameraUnavailable
    pipeline,original=full_scene()
    pipeline.config['search_object_box']=dict(min=[-.015,-.015,0],max=[.015,.015,.06])
    pipeline.segment=ConfirmedMask(pipeline.segment)
    monkeypatch.setattr(pipeline,'_primary_evidence',lambda *a,**kw:(np.eye(4),True,False,True))
    try:
        value=pipeline.process(sample(original,0),base_eef=np.eye(4),
            secondary_error=SecondaryCameraUnavailable('secondary_camera_pairing_unavailable'))
        assert value.objects[0].pose is not None and value.objects[0].held is True
        assert value.local_collision_evidence is None
    finally:pipeline.close()

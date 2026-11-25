"""Transport can sweep the registered bucket again when its measurement source changes."""
import pytest

from f1_harness.contracts import FixturePose, HarnessError
from f1_harness.revalidation import (_measurement_identity, _scene_unchanged, tolerances,
    TRANSPORT_MEASUREMENT_PENDING, RevalidationEvidenceUpdated, RevalidationSceneChanged)
from test_full_development_skills import scene
from test_plan_revalidation import scenario, with_left_image


def loaded(obs):
    target,hole=obs.objects
    target=target.model_copy(update={'held':True, 'eef_object_pose':tuple(scene().eef_object.ravel())})
    return with_left_image(obs.model_copy(update={'objects':(target,hole)}),measured=True)


def with_fixture(obs, *, retained=False):
    target,hole=obs.objects
    body=FixturePose(fixture_id='bucket',frame_id=hole.frame_id,pose=hole.pose,reference_pose=hole.pose,
        evidence_ids=tuple(f.blob_sha256 for f in obs.frames if f.camera=='head'),measured_at_ns=obs.received_ns)
    if not retained:
        return obs.model_copy(update={'fixture':body})
    return obs.model_copy(update={'fixture':body.model_copy(update={'source':'retained_static_fixture'}),
        'objects':(target,), 'frames':tuple(f for f in obs.frames if f.camera=='right'),
        'pairings':tuple(p for p in obs.pairings if p.camera=='right')})


@pytest.mark.parametrize('restored_from_retained',[False,True])
def test_transport_resweeps_registered_reference_and_measured_cross_view_aperture(scenario, restored_from_retained):
    serv,_,_,a,b,_=scenario
    before,after=loaded(a),loaded(b)
    if restored_from_retained:
        before=with_fixture(before,retained=True)
    else:
        after=with_fixture(after)
    assert _measurement_identity(before,after,task=serv.task,skill='transfer_above_hole') is True
    _scene_unchanged(before,after,task=serv.task,threshold=tolerances(serv.fixture,'transfer_above_hole'),
                     skill='transfer_above_hole',resweep=True)


def test_changed_stream_and_misbinding_still_reject_reference_switch(scenario):
    serv,_,_,a,b,_=scenario
    before=with_fixture(loaded(a),retained=True)
    direct=loaded(b)
    after=direct.model_copy(update={'frames':tuple(f.model_copy(update={'source_epoch':f.source_epoch+1})
        if f.camera=='right' else f for f in direct.frames)})
    with pytest.raises(HarnessError,match='revalidation_camera_identity_changed'):
        _measurement_identity(before,after,task=serv.task,skill='transfer_above_hole')
    hole=direct.objects[1].model_copy(update={'evidence_ids':(*direct.objects[1].evidence_ids[:2],'b'*64)})
    with pytest.raises(RevalidationSceneChanged,match='revalidation_scene_evidence_changed'):
        _measurement_identity(before,direct.model_copy(update={'objects':(direct.objects[0],hole)}),
                              task=serv.task,skill='transfer_above_hole')


@pytest.mark.parametrize('source',['untraced_retention','repeated_image','missing_current_reference'])
def test_incomparable_transport_measurement_waits_without_masking_release(scenario,source):
    serv,_,_,a,b,_=scenario
    before=loaded(a)
    after=with_fixture(loaded(b),retained=True)
    if source=='repeated_image':
        before=with_fixture(before)
        after=after.model_copy(update={'fixture':before.fixture.model_copy(update={
            'source':'retained_static_fixture','measured_at_ns':a.received_ns+1})})
    elif source=='missing_current_reference':
        after=after.model_copy(update={'fixture':None,'geometry_scope':'coarse_object_verification'})
    with pytest.raises(RevalidationEvidenceUpdated, match=TRANSPORT_MEASUREMENT_PENDING):
        _measurement_identity(before,after,task=serv.task,skill='transfer_above_hole')
    dropped=after.objects[0].model_copy(update={'held':False,'eef_object_pose':None})
    with pytest.raises(RevalidationSceneChanged,match='revalidation_attachment_changed'):
        _measurement_identity(before,after.model_copy(update={'objects':(dropped,)}),
                              task=serv.task,skill='transfer_above_hole')

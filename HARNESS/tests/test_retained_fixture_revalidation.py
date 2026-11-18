"""A rejected head sample retains static provenance, never current aperture evidence."""
import pytest

from f1_harness.contracts import FixturePose, HarnessError, digest
from f1_harness.engine import UNPUBLISHED_SCENE_CODES
from f1_harness.observation import require_geometry_identity
from f1_harness.owner_bridge import JointProposal
from f1_harness.revalidation import (
    _measurement_identity, proof_observation, revalidate_candidate, RevalidationSceneChanged,
)
from test_plan_revalidation import scenario, with_left_image


def pair(original, latest):
    fixture = FixturePose(fixture_id='bucket', frame_id='native',
        pose=original.objects[1].pose, reference_pose=original.objects[1].pose,
        evidence_ids=('original-head-rgb', 'original-head-depth'), measured_at_ns=original.received_ns)
    original = original.model_copy(update={'fixture': fixture})
    latest = latest.model_copy(update=dict(
        fixture=fixture.model_copy(update={'source': 'retained_static_fixture'}),
        objects=(latest.objects[0],),
        frames=tuple(f for f in latest.frames if f.camera == 'right'),
        pairings=tuple(p for p in latest.pairings if p.camera == 'right'),
        quality_warnings=('static_fixture_prediction_from_prior_measurement',)))
    return original, latest


def prepared(scenario):
    serv, args, clock, original, latest, intent = scenario
    original, latest = pair(original, latest)
    serv.fixture.update(independent_fixture_localization=True,
                        fixture_reference_hole=list(original.fixture.reference_pose))
    args.update(original_observation=original.model_dump(), observation=latest.model_dump())
    return serv, args, clock, original, latest, intent


def test_full_to_retained_revalidates_actual_path_and_owner_proof(scenario):
    serv, args, clock, original, latest, intent = prepared(scenario)
    proof = revalidate_candidate(serv, **args)
    assert proof['geometry_checks']['swept_segments'] > 0
    assert proof['observation_sha256'] == digest(latest)
    assert proof['original_observation_sha256'] == digest(original)
    assert proof['observation']['fixture']['measured_at_ns'] == original.received_ns
    assert latest.received_ns > original.fixture.measured_at_ns
    verified = proof_observation(proof, proposal=JointProposal(**args['proposal']), original=original,
        intent=intent, task=serv.task, now_ns=clock[0], max_age_ns=500_000_000,
        live_observation=latest)
    assert verified == latest


def test_retained_transit_ignores_left_used_only_for_aperture(scenario):
    serv, args, _, original, latest, intent = prepared(scenario)
    original = with_left_image(original, measured=True)
    args.update(original_observation=original.model_dump())
    proof = revalidate_candidate(serv, **args)
    assert proof['original_observation_sha256'] == digest(original)
    assert proof['geometry_checks']['swept_segments'] > 0
    _measurement_identity(latest, original, task=serv.task, skill=intent.skill)


@pytest.mark.parametrize('field,value', [
    ('fixture_id', 'other'), ('frame_id', 'different-base'),
    ('evidence_ids', ('substituted-evidence',)), ('measured_at_ns', 2),
    ('pose', tuple([1.] * 16)), ('reference_pose', tuple([1.] * 16)),
])
def test_retained_fixture_must_have_exact_original_lineage(scenario, field, value):
    serv, args, _, _, latest, _ = prepared(scenario)
    changed = latest.model_copy(update={'fixture': latest.fixture.model_copy(update={field: value})})
    args['observation'] = changed.model_dump()
    with pytest.raises(HarnessError, match='retained_fixture_lineage_changed'):
        revalidate_candidate(serv, **args)


def test_intermediate_new_fixture_measurement_is_swept_without_repeating_ik(scenario):
    serv, args, _, original, latest, _ = prepared(scenario)
    measured = latest.fixture.model_copy(update={
        'measured_at_ns': original.received_ns+1_000_000,
        'evidence_ids': ('intermediate-head-rgb', 'intermediate-head-depth')})
    args['observation'] = latest.model_copy(update={'fixture': measured}).model_dump()
    proof=revalidate_candidate(serv, **args)
    assert proof['geometry_checks']['swept_segments']>0
    assert proof['observation']['fixture']['measured_at_ns']==measured.measured_at_ns


@pytest.mark.parametrize('field,value', [
    ('source_epoch', 2), ('publisher_id', 'new-publisher'),
    ('camera_info_sha256', '2' * 64), ('frame_id', 'different-frame'),
])
def test_retained_fixture_never_relaxes_current_wrist_identity(scenario, field, value):
    serv, args, _, _, latest, _ = prepared(scenario)
    changed = latest.model_copy(update={'frames': tuple(
        f.model_copy(update={field: value}) for f in latest.frames)})
    args['observation'] = changed.model_dump()
    with pytest.raises(HarnessError, match='camera_identity_changed'):
        revalidate_candidate(serv, **args)


@pytest.mark.parametrize('field,value', [
    ('host_boot_id', 'new-boot'), ('acquisition_instance', 'new-acquisition'),
    ('clock_domain', 'other-clock'), ('calibration_id', 'other-calibration'),
    ('geometry_camera', 'head'), ('geometry_scope', 'coarse_object_verification'),
])
def test_retained_fixture_never_relaxes_measurement_scope(scenario, field, value):
    serv, _, _, original, latest, intent = prepared(scenario)
    changed = latest.model_copy(update={field: value})
    with pytest.raises(HarnessError, match='measurement_identity_changed'):
        _measurement_identity(original, changed, task=serv.task, skill=intent.skill)


def test_restored_head_with_same_fixture_geometry_keeps_valid_sweep(scenario):
    restored = scenario[4]
    serv, args, clock, original, latest, intent = prepared(scenario)
    proof = revalidate_candidate(serv, **args)
    restored = restored.model_copy(update={'fixture': original.fixture.model_copy(update={
        'measured_at_ns': restored.received_ns, 'evidence_ids': ('new-head-rgb', 'new-head-depth')})})
    assert proof_observation(proof, proposal=JointProposal(**args['proposal']), original=original,
        intent=intent, task=serv.task, now_ns=clock[0], max_age_ns=500_000_000,
        live_observation=restored)==latest
    # It can become the next planning observation with its own full source identity.
    assert require_geometry_identity(restored, goal=serv.task.goal,
        admission_profile=serv.task.admission_profile, skill='approach_grasp') == ('head', 'right')


@pytest.mark.parametrize('skill', ['align', 'insert', 'release', 'verify_placed', 'retreat', None])
def test_retained_fixture_cannot_supply_precision_or_unspecified_stage(scenario, skill):
    serv, _, _, _, latest, _ = prepared(scenario)
    with pytest.raises(ValueError, match='full_scene_required_for_motion'):
        require_geometry_identity(latest, goal=serv.task.goal,
            admission_profile=serv.task.admission_profile, skill=skill)


@pytest.mark.parametrize('skill', ['approach_grasp', 'grasp', 'regrasp', 'transfer_above_hole',
    'move_tcp', 'recover_grasp', 'verify_grasp', 'inspect_in_hand', 'reobserve'])
def test_current_object_and_static_obstacles_admit_only_supported_stages(scenario, skill):
    serv, _, _, _, latest, _ = prepared(scenario)
    assert require_geometry_identity(latest, goal=serv.task.goal,
        admission_profile=serv.task.admission_profile, skill=skill) == ('right',)


@pytest.mark.parametrize('change', ['retained', 'fixture_moved'])
def test_owner_consumes_retained_scene_or_replans_changed_fixture(tmp_path, change):
    from test_plan_revalidation import make_owner, observation
    engine, bridge, serv, clock, state, poll, original = make_owner(tmp_path)
    original, _ = pair(original, original)
    engine.update_observation(original)
    serv.fixture.update(independent_fixture_localization=True,
                        fixture_reference_hole=list(original.fixture.reference_pose))
    old = engine.pending
    try:
        assert poll() is None
        future, func, args = bridge.executor.jobs[-1]
        candidate = func(*args)
        clock[0] += 8_000_000_000
        future.set_result(candidate)
        assert poll() is None and bridge.proposal is None
        clock[0] += 100_000_000
        latest = observation(clock[0], boot=engine.host_boot_id, name='fresh-after-plan')
        if change == 'retained':
            _, latest = pair(original, latest)
        else:
            moved = list(original.fixture.pose)
            moved[3] += .01
            latest = latest.model_copy(update={'fixture': original.fixture.model_copy(update={
                'pose': tuple(moved), 'measured_at_ns': latest.received_ns,
                'evidence_ids': ('new-head-rgb', 'new-head-depth')})})
        engine.update_live_observation(latest, engine.status()['perception_context'])
        assert poll() is None and len(bridge.executor.jobs) == 2
        future, func, args = bridge.executor.jobs[-1]
        try:
            future.set_result(func(*args))
        except RevalidationSceneChanged as error:
            future.set_exception(error)
        target = poll()
        assert engine.authorized and not engine.hold_requested
        assert not engine.request_publications
        assert target is not None
        assert bridge.validate_first_publication(**state, stamps16=(clock[0],)*16, now_ns=clock[0])
        assert bridge.revalidation_proof['observation_sha256'] == digest(latest)
        assert bridge.revalidation_proof['geometry_checks']['swept_segments']>0
        assert bridge.plan_observation == original
    finally:
        bridge.close()


@pytest.mark.parametrize('prefix', ['plan_revalidation_rejected:', 'revalidation_proof_rejected:'])
@pytest.mark.parametrize('publication_count', [0, 1])
def test_fixture_change_does_not_allow_unknown_or_published_request_replay(tmp_path, prefix, publication_count):
    from test_unpublished_plan import make_bridge
    engine, bridge, _, _, poll, _ = make_bridge(tmp_path)
    try:
        assert poll() is None
        engine.request_publications['one-request'] = {'publication_count': publication_count}
        with pytest.raises(HarnessError, match='unpublished_replan_not_eligible'):
            engine.reject_unpublished_for_replan('one-request',
                reason=prefix+'revalidation_fixture_changed')
        assert engine.running.request_id == 'one-request'
    finally:
        bridge.close()

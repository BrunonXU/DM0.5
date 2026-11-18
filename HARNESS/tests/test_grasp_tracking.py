"""Grasp following must survive closing transients without accepting a grasp miss."""
import math

import numpy as np
import pytest

from f1_harness.grasp_tracking import following_evidence, nonfollowing_evidence, quarantine_following, revalidate_following


def measured(index, *, eef_z=0., object_z=None, relative_x=0., gripper=100., yaw=0.,
             capture=None, feedback=None):
    eef = np.eye(4)
    eef[2, 3] = eef_z
    obj = np.eye(4)
    obj[2, 3] = eef_z if object_z is None else object_z
    obj[0, 3] = relative_x
    c, s = math.cos(yaw), math.sin(yaw)
    obj[:2, :2] = [[c, -s], [s, c]]
    stamp = (index + 1) * 100_000_000
    capture = stamp if capture is None else capture
    return dict(time=stamp, state_ns=stamp if feedback is None else feedback,
                camera_identity=('acquisition', 'right', (('rgb', 0, None), ('depth', 0, None))),
                source_stamps=(capture, capture), eef=eef, obj=obj,
                relative=np.linalg.inv(eef) @ obj, gripper=gripper)


def evaluate(rows):
    return following_evidence(rows, translation_tolerance=.005,
                              rotation_tolerance=math.radians(5), gripper_tolerance=.5)


def test_reseating_prefix_cannot_poison_a_later_contiguous_lift():
    rows = [measured(0, relative_x=.03, gripper=20),
            measured(1, relative_x=.015, gripper=70),
            *[measured(i + 2, eef_z=z, relative_x=.002) for i, z in enumerate((0., .02, .04))]]
    # The old whole-window maximum rejects this real distinction between closing
    # and a subsequent stable attachment; the new proof uses only its latest run.
    assert max(np.linalg.norm(r['relative'][:3, 3] - rows[-1]['relative'][:3, 3]) for r in rows) > .005
    result = evaluate(rows)
    assert result['accepted'] and result['support_samples'] == 3
    assert result['motion_m'] == pytest.approx(.04)
    assert result['relative_translation_m'] == pytest.approx(0)
    assert result['gripper_span'] == 0
    assert result['boundary_reason'] == 'relative_translation_changed'


@pytest.mark.parametrize('case', ['empty_grasp', 'stationary_arm', 'closing', 'duplicate_camera', 'duplicate_feedback'])
def test_failed_or_non_independent_evidence_never_proves_hold(case):
    rows = []
    for i, z in enumerate((0., .02, .04)):
        kwargs = {}
        if case == 'empty_grasp': kwargs['object_z'] = 0.
        if case == 'stationary_arm': z = 0.
        if case == 'closing': kwargs['gripper'] = 20. + i * 30.
        if case == 'duplicate_camera': kwargs['capture'] = 1
        if case == 'duplicate_feedback': kwargs['feedback'] = 1
        rows.append(measured(i, eef_z=z, **kwargs))
    assert not evaluate(rows)['accepted']


def test_latest_incompatible_sample_cannot_be_skipped_to_claim_earlier_following():
    rows = [measured(i, eef_z=z) for i, z in enumerate((0., .02, .04))]
    assert evaluate(rows)['accepted']
    rows.append(measured(3, eef_z=.06, relative_x=.03))
    result = evaluate(rows)
    assert not result['accepted'] and result['support_samples'] == 1
    assert result['boundary_reason'] == 'relative_translation_changed'


def test_closure_changes_are_not_stable_following_even_with_fixed_relative_pose():
    rows = [measured(i, eef_z=i * .02, gripper=90. + i * 5.) for i in range(3)]
    result = evaluate(rows)
    assert not result['accepted'] and result['boundary_reason'] == 'gripper_not_stable'
    assert result['boundary_metrics']['gripper_span'] == 5.


def test_existing_rotation_tolerance_is_retained_and_reported():
    rows = [measured(i, eef_z=i * .02, yaw=math.radians(i * 4.3)) for i in range(3)]
    result = evaluate(rows)
    assert not result['accepted'] and result['support_samples'] == 2
    assert result['boundary_reason'] == 'relative_rotation_changed'
    assert result['boundary_metrics']['relative_rotation_rad'] == pytest.approx(math.radians(8.6))


def test_coarse_position_following_does_not_require_precision_yaw():
    rows=[measured(i,eef_z=i*.02,yaw=math.radians(i*12)) for i in range(3)]
    result=following_evidence(rows,translation_tolerance=.015,rotation_tolerance=None,gripper_tolerance=.5)
    assert result['accepted'] and result['relative_rotation_rad']==pytest.approx(math.radians(24))
    assert not evaluate(rows)['accepted']  # strict pose evidence remains available


@pytest.mark.parametrize('case',['empty','still','duplicate','closing'])
def test_coarse_following_cannot_convert_missing_physical_evidence_to_hold(case):
    rows=[]
    for i in range(3):
        kw=dict(eef_z=i*.02,yaw=math.radians(i*12))
        if case=='empty':kw['object_z']=0.
        if case=='still':kw['eef_z']=0.
        if case=='duplicate':kw['capture']=1
        if case=='closing':kw['gripper']=20.+i*40
        rows.append(measured(i,**kw))
    assert not following_evidence(rows,translation_tolerance=.015,
        rotation_tolerance=None,gripper_tolerance=.5)['accepted']


def test_miss_is_measured_world_stationarity_during_hand_motion():
    rows=[measured(i,eef_z=i*.02,object_z=0.) for i in range(3)]
    result=nonfollowing_evidence(rows,translation_tolerance=.015,gripper_tolerance=.5)
    assert result['accepted'] and result['reason']=='measured_object_did_not_follow_hand'
    assert result['object_world_displacement_m']==0
    assert not nonfollowing_evidence(rows,translation_tolerance=.015,gripper_tolerance=None)['accepted']


@pytest.mark.parametrize('case',['held','still','closing','duplicate','camera_changed','latest_moved'])
def test_ambiguous_or_attached_samples_cannot_prove_miss(case):
    rows=[measured(i,eef_z=i*.02,object_z=0.) for i in range(3)]
    if case=='held':rows=[measured(i,eef_z=i*.02) for i in range(3)]
    if case=='still':rows=[measured(i) for i in range(3)]
    if case=='closing':rows=[measured(i,eef_z=i*.02,object_z=0.,gripper=i*20.) for i in range(3)]
    if case=='duplicate':rows=[measured(i,eef_z=i*.02,object_z=0.,capture=1) for i in range(3)]
    if case=='camera_changed':rows[-1]['camera_identity']=('other',)
    if case=='latest_moved':rows[-1]['obj'][0,3]=.1
    assert not nonfollowing_evidence(rows,translation_tolerance=.015,gripper_tolerance=.5)['accepted']


def test_square_symmetry_does_not_look_like_slip():
    rows = [measured(i, eef_z=i * .02, yaw=i * math.pi / 2) for i in range(3)]
    assert evaluate(rows)['accepted']


@pytest.mark.parametrize('change', ['camera', 'epoch', 'acquisition', 'source_reversal'])
def test_segment_does_not_cross_camera_identity_or_reversed_time(change):
    rows = [measured(i, eef_z=i * .02) for i in range(3)]
    if change == 'source_reversal': rows[-1]['source_stamps'] = (1, 1)
    else:
        identity = list(rows[-1]['camera_identity'])
        if change == 'camera': identity[1] = 'head'
        if change == 'acquisition': identity[0] = 'next-acquisition'
        if change == 'epoch': identity[2] = (('rgb', 1, None), ('depth', 1, None))
        rows[-1]['camera_identity'] = tuple(identity)
    assert not evaluate(rows)['accepted']


def pipeline_fixture(monkeypatch):
    from f1_harness.contracts import SceneObject
    from test_full_development_scene import full_scene, sample
    from test_phase_perception import context
    pipe, observation = full_scene()
    pose = np.eye(4)
    pipe.config['face_object'] = np.eye(4).tolist()
    def object_measurement(**kwargs):
        return SceneObject(object_id='block', shape='square', pose=tuple(pose.ravel()),
                           frame_id='test-base', uncertainty_m=.001, evidence_ids=('measured-test',))
    monkeypatch.setattr('f1_harness.scene_pipeline.object_from_mask', object_measurement)
    def push(index, *, z=0., relative_x=0., grip=100., valid=True, epoch=1, phase='grasp',alter=None):
        pose[:] = np.eye(4); pose[2, 3] = z; pose[0, 3] = relative_x
        eef = np.eye(4); eef[2, 3] = z
        obs = sample(observation, index)
        state = list(obs.state16); state[15] = grip; state[7] = z * 100
        mask = list(obs.state_valid_mask); mask[15] = valid
        obs = obs.model_copy(update=dict(state16=tuple(state), state_valid_mask=tuple(mask)))
        if alter is not None:obs=alter(obs)
        return pipe.process(obs, base_eef=eef, context=context(phase, epoch=epoch))
    return pipe, push


def test_pipeline_uses_current_segment_then_retains_proof_while_stationary(monkeypatch):
    pipe, push = pipeline_fixture(monkeypatch)
    try:
        push(0, relative_x=.03, grip=20)
        push(1, relative_x=.015, grip=70)
        for i, z in enumerate((0., .02, .04), 2): result = push(i, z=z)
        assert result.objects[0].held is True and result.objects[0].eef_object_pose is not None
        assert pipe.last_timing['primary_evidence']['support_samples'] == 3
        # Motion leaves the old two-second window; the measured attachment proof
        # remains valid under fresh matching samples, rather than needing a poke.
        for i in range(5, 28): result = push(i, z=.04, phase='verify_grasp')
        assert result.objects[0].held is True
        diag = pipe.last_timing['primary_evidence']
        assert diag['motion_m'] == 0 and diag['reason'] == 'measured_following_retained'
    finally: pipe.close()


@pytest.mark.parametrize('change', ['open', 'invalid_gripper', 'changed_gripper', 'epoch'])
def test_pipeline_invalidates_existing_proof_when_evidence_scope_changes(monkeypatch, change):
    pipe, push = pipeline_fixture(monkeypatch)
    try:
        for i, z in enumerate((0., .02, .04)): result = push(i, z=z)
        assert result.objects[0].held is True
        options = {'z': .04, 'phase': 'verify_grasp'}
        if change == 'open': options['grip'] = 0.
        if change == 'invalid_gripper': options['valid'] = False
        if change == 'changed_gripper': options['grip'] = 98.
        if change == 'epoch': options['epoch'] = 2
        result = push(9 if change == 'gap' else 3, **options)
        assert result.objects[0].held is not True
        assert pipe.in_hand_reference is None and pipe.in_hand_gripper is None
    finally: pipe.close()


def test_one_outlier_is_unknown_until_three_new_matching_pairs_revalidate_original_proof(monkeypatch):
    pipe, push = pipeline_fixture(monkeypatch)
    try:
        for i, z in enumerate((0., .02, .04)): result = push(i, z=z)
        reference = pipe.in_hand_reference.copy()
        assert result.objects[0].held is True
        result = push(3, z=.04, relative_x=.02, phase='verify_grasp')
        assert result.objects[0].held is None and result.objects[0].eef_object_pose is None
        assert pipe.in_hand_reference is None and pipe.in_hand_quarantine is not None
        deadline = pipe.in_hand_quarantine['expires_ns']
        for i in (4, 5):
            result = push(i, z=.04, phase='verify_grasp')
            assert result.objects[0].held is None
            assert pipe.in_hand_quarantine['expires_ns'] == deadline
        result = push(6, z=.04, phase='verify_grasp')
        assert result.objects[0].held is True and result.objects[0].eef_object_pose is not None
        np.testing.assert_array_equal(pipe.in_hand_reference, reference)
        assert pipe.in_hand_quarantine is None
        assert pipe.in_hand_recovery_deadline_ns is None
        diagnostic = pipe.last_timing['primary_evidence']
        assert diagnostic['quarantine_support_samples'] == 3
        assert diagnostic['reason'] == 'measured_following_revalidated'
    finally: pipe.close()


def test_ambiguous_changed_attachment_suspends_current_hold_but_keeps_history(monkeypatch):
    pipe, push = pipeline_fixture(monkeypatch)
    try:
        for i, z in enumerate((0., .02, .04)): push(i, z=z)
        push(3, z=.04, relative_x=.02, phase='verify_grasp')
        deadline = pipe.in_hand_quarantine['expires_ns']
        for i in range(4, 25):
            result = push(i, z=.04, relative_x=.02, phase='verify_grasp')
            assert result.objects[0].held is None
            if pipe.in_hand_quarantine is not None:
                assert pipe.in_hand_quarantine['expires_ns'] == deadline
                assert not pipe.in_hand_quarantine['support']
        assert pipe.in_hand_quarantine is not None and deadline is None
        # Three fresh consistent pairs can restore history after a long wait.
        for i in (25,26):assert push(i,z=.04,phase='verify_grasp').objects[0].held is None
        result=push(27,z=.04,phase='verify_grasp')
        assert result.objects[0].held is True
    finally: pipe.close()


def test_new_confirmed_samples_allow_a_later_independent_recovery(monkeypatch):
    pipe, push = pipeline_fixture(monkeypatch)
    try:
        for i, z in enumerate((0., .02, .04)): push(i, z=z)
        push(3, z=.04, relative_x=.02, phase='verify_grasp')
        for i in range(4, 23): result = push(i, z=.04, phase='verify_grasp')
        assert result.objects[0].held is True
        push(23, z=.04, relative_x=.02, phase='verify_grasp')
        for i in range(24, 27): result = push(i, z=.04, phase='verify_grasp')
        assert result.objects[0].held is True
    finally: pipe.close()


def test_bad_visual_frame_and_duplicate_invalidation_preserve_recoverable_proof(monkeypatch):
    pipe, push = pipeline_fixture(monkeypatch)
    try:
        for i, z in enumerate((0., .02, .04)): push(i, z=z)
        for _ in range(2):pipe.reject_measurement('supported_upright_face_not_observed')
        assert pipe.in_hand_reference is None and pipe.in_hand_quarantine is not None
        for i in (3,4):
            assert push(i,z=.04,phase='inspect_in_hand').objects[0].held is None
        result=push(5,z=.04,phase='inspect_in_hand')
        assert result.objects[0].held is True and result.objects[0].eef_object_pose is not None
    finally:pipe.close()


def test_explicit_identity_reset_discards_attachment(monkeypatch):
    pipe,push=pipeline_fixture(monkeypatch)
    try:
        for i,z in enumerate((0.,.02,.04)):push(i,z=z)
        pipe.invalidate()
        for i in (3,4,5):result=push(i,z=.04,phase='inspect_in_hand')
        assert result.objects[0].held is None and pipe.in_hand_quarantine is None
    finally:pipe.close()


@pytest.mark.parametrize('change',['calibration','opening','missing_frame'])
def test_bad_pair_does_not_hide_real_invalidation_or_treat_missing_frame_as_identity_change(monkeypatch,change):
    pipe,push=pipeline_fixture(monkeypatch)
    try:
        for i,z in enumerate((0.,.02,.04)):push(i,z=z)
        def alter(obs):
            frames=obs.frames
            if change=='calibration':frames=tuple(f.model_copy(update={'camera_info_sha256':'9'*64}) for f in frames)
            if change=='missing_frame':frames=tuple(f for f in frames if f.camera!='head')
            return obs.model_copy(update=dict(frames=frames,pairings=()))
        with pytest.raises(ValueError):push(3,z=.04,grip=0. if change=='opening' else 100.,alter=alter)
        retained=pipe.in_hand_reference is not None or pipe.in_hand_quarantine is not None
        assert retained is (change=='missing_frame')
    finally:pipe.close()


def test_unavailable_gripper_suspends_but_fresh_unchanged_grip_can_revalidate(monkeypatch):
    pipe,push=pipeline_fixture(monkeypatch)
    try:
        for i,z in enumerate((0.,.02,.04)):push(i,z=z)
        assert push(3,z=.04,valid=False).objects[0].held is None
        assert pipe.in_hand_reference is None and pipe.in_hand_quarantine is not None
        for i in (4,5):assert push(i,z=.04).objects[0].held is None
        assert push(6,z=.04).objects[0].held is True
    finally:pipe.close()


@pytest.mark.parametrize('reason',['unverified_pairing','new_consistent_source_frames_required',
                                 'unknown_camera_measurement_error'])
def test_bad_pair_after_temporal_gap_does_not_erase_stationary_grasp(monkeypatch,reason):
    pipe,push=pipeline_fixture(monkeypatch)
    try:
        for i,z in enumerate((0.,.02,.04)):push(i,z=z)
        pipe.reject_measurement('frame_feedback_skew:recorded_temporal_pair_failure')
        pipe.reject_measurement(reason)
        for i in (170,171):assert push(i,z=.04,phase='inspect_in_hand').objects[0].held is None
        assert push(172,z=.04,phase='inspect_in_hand').objects[0].held is True
    finally:pipe.close()


def test_visible_object_taken_away_from_stationary_hand_is_not_permanent_held(monkeypatch):
    pipe,push=pipeline_fixture(monkeypatch)
    pipe.config['search_object_box']={'min':[-.015,-.015,0.],'max':[.015,.015,.06]}
    try:
        for i,z in enumerate((0.,.02,.04)):push(i,z=z)
        for i in (3,4,5):
            assert push(i,z=.04,relative_x=.10,phase='transfer_above_hole').objects[0].held is None
        assert push(6,z=.04,relative_x=.10,phase='transfer_above_hole').objects[0].held is False
        # It remains a measured departure while a human moves the object farther.
        assert push(7,z=.04,relative_x=.15,phase='transfer_above_hole').objects[0].held is False
        assert pipe.in_hand_reference is None and pipe.in_hand_quarantine is None
    finally:pipe.close()


@pytest.mark.parametrize('change', ['open', 'changed_gripper', 'epoch'])
def test_invalid_scope_or_gripper_discards_quarantine_permanently(monkeypatch, change):
    pipe, push = pipeline_fixture(monkeypatch)
    try:
        for i, z in enumerate((0., .02, .04)): push(i, z=z)
        push(3, z=.04, relative_x=.02, phase='verify_grasp')
        assert pipe.in_hand_quarantine is not None
        options = {'z': .04, 'phase': 'verify_grasp'}
        if change == 'open': options['grip'] = 0.
        if change == 'invalid_gripper': options['valid'] = False
        if change == 'changed_gripper': options['grip'] = 98.
        if change == 'epoch': options['epoch'] = 2
        index = 30 if change == 'gap' else 4
        result = push(index, **options)
        assert result.objects[0].held is not True and pipe.in_hand_quarantine is None
        for i in range(index + 1, index + 5):
            result = push(i, z=.04, phase='verify_grasp', epoch=options.get('epoch', 1))
        assert result.objects[0].held is None
    finally: pipe.close()


def test_static_observations_cannot_create_a_quarantined_first_grasp(monkeypatch):
    pipe, push = pipeline_fixture(monkeypatch)
    try:
        for i in range(8):
            result = push(i, z=.04, relative_x=.02 if i == 3 else 0., phase='verify_grasp')
            assert result.objects[0].held is None
            assert pipe.in_hand_reference is None and pipe.in_hand_quarantine is None
    finally: pipe.close()


def recheck(proof, row):
    return revalidate_following(proof, row, translation_tolerance=.005,
                                rotation_tolerance=math.radians(5), gripper_tolerance=.5)


@pytest.mark.parametrize('duplicate', ['camera', 'feedback'])
def test_quarantine_requires_independent_camera_and_feedback_pairs(duplicate):
    reference = measured(2, eef_z=.04)['relative']
    bad = measured(3, eef_z=.04, relative_x=.02)
    proof = quarantine_following(reference, 100., bad, expires_ns=2_400_000_000)
    first = measured(4, eef_z=.04)
    assert not recheck(proof, first)['restored']
    for index in (5, 6, 7):
        row = measured(index, eef_z=.04,
                       **({'capture': first['source_stamps'][0]} if duplicate == 'camera'
                          else {'feedback': first['state_ns']}))
        result = recheck(proof, row)
        assert not result['restored'] and result['quarantine_support_samples'] == 1
    assert proof['expires_ns'] == 2_400_000_000


def test_matching_samples_must_be_consecutive_and_rotation_still_uses_original_limit():
    reference = measured(2, eef_z=.04)['relative']
    proof = quarantine_following(reference, 100., measured(3, eef_z=.04, yaw=.2),
                                 expires_ns=2_400_000_000)
    assert recheck(proof, measured(4, eef_z=.04))['quarantine_support_samples'] == 1
    assert recheck(proof, measured(5, eef_z=.04))['quarantine_support_samples'] == 2
    result = recheck(proof, measured(6, eef_z=.04, yaw=math.radians(6)))
    assert not result['restored'] and result['quarantine_support_samples'] == 0
    for i in (7, 8): assert not recheck(proof, measured(i, eef_z=.04))['restored']
    assert recheck(proof, measured(9, eef_z=.04))['restored']


@pytest.mark.parametrize('change', ['camera', 'source_epoch', 'owner_scope', 'acquisition', 'feedback_missing'])
def test_quarantine_identity_change_is_not_a_revalidation(change):
    base = measured(3, eef_z=.04)
    proof = quarantine_following(base['relative'], 100., base, expires_ns=2_400_000_000)
    row = measured(4, eef_z=.04)
    identity = list(row['camera_identity'])
    if change == 'camera': identity[1] = 'head'
    if change == 'source_epoch': identity[2] = (('rgb', 1, None), ('depth', 1, None))
    if change == 'acquisition': identity[0] = 'new-acquisition'
    row['camera_identity'] = tuple(identity)
    if change == 'owner_scope': row['scope_identity'] = ('new-epoch',)
    if change == 'feedback_missing': row['state_ns'] = None
    result = recheck(proof, row)
    assert result['discard'] and not result['restored']


@pytest.mark.parametrize('case',['independent','duplicate_frame','duplicate_feedback','source_epoch','expired','grip_changed'])
def test_associated_camera_handoff_keeps_original_proof_limits(case):
    from f1_harness.grasp_tracking import handoff_reference
    base=measured(3,eef_z=.04)
    deadline=700_000_000 if case=='expired' else 2_400_000_000
    original=quarantine_following(base['relative'],100.,base,expires_ns=deadline)
    camera=('acquisition','head',(('rgb',0,None),('depth',0,None)))
    proof=handoff_reference(original,camera)
    assert proof['started_ns']==original['started_ns'] and proof['expires_ns']==deadline
    assert proof['camera_identity']==original['camera_identity']
    for i in (4,5,6):
        # New view starts on its own source clock, below the wrist's timestamp.
        row=measured(i,eef_z=.04,capture=1 if case=='duplicate_frame' else i,
            feedback=500_000_000 if case=='duplicate_feedback' else None,
            gripper=110. if case=='grip_changed' else 100.)
        row['camera_identity']=camera
        if case=='source_epoch':
            row['camera_identity']=('acquisition','head',(('rgb',1,None),('depth',1,None)))
        result=recheck(proof,row)
        if i<6:assert not result['restored']
    assert result['restored']==(case=='independent')
    if case in ('source_epoch','expired','grip_changed'):assert result['discard']


@pytest.mark.parametrize('reason',['feedback_expired:right','mask_uncertain_requires_new_prompt:right','frame_feedback_skew'])
def test_feedback_or_visual_gap_preserves_proof_but_requires_new_evidence(monkeypatch,reason):
    pipe,push=pipeline_fixture(monkeypatch)
    try:
        for i,z in enumerate((0.,.02,.04)):push(i,z=z)
        pipe.reject_measurement(reason)
        assert pipe.in_hand_reference is None
        assert pipe.in_hand_quarantine is not None
        for i in (100,101):
            assert push(i,z=.04,phase='verify_grasp').objects[0].held is None
        assert push(102,z=.04,phase='verify_grasp').objects[0].held is True
    finally:pipe.close()

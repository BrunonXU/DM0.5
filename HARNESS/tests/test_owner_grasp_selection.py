"""Pitch selection crosses skills only after actual owner arrival and verification."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from f1_harness.contracts import HarnessError, SceneObject, SkillResult
from f1_harness.control import build_bridge
from f1_harness.owner_bridge import JointProposal
from test_unpublished_plan import make_bridge, finish_plan
from test_unpublished_retry import fresh_observation


DESCRIPTOR = dict(strategy='tcp_pitch', pitch_offset_deg=30.)
# Keep a nontrivial square coordinate gauge; owner must not rewrite its yaw.
POSE = (0., -1., 0., .4, 1., 0., 0., -.2, 0., 0., 1., .1, 0., 0., 0., 1.)


def prepared(tmp_path):
    e, b, clock, state, poll, obs = make_bridge(tmp_path)
    obs = obs.model_copy(update=dict(objects=(SceneObject(object_id='block', shape='square',
        pose=POSE, frame_id='right_arm_native', held=False),)))
    e.update_observation(obs)
    original = e.pending
    assert poll() is None
    future = b.future
    intent, _, full, held = b.executor.jobs[-1][2]
    target = list(held); target[7] += .1
    plan = JointProposal(intent.request_id, intent.epoch, intent.scene_version, intent.calibration_id,
        full, (tuple(target),), .1, 100_000_000, .5,
        geometry_trace=(dict(start_index=0, end_index=1, grasp_candidate_descriptor=deepcopy(DESCRIPTOR)),),
        candidate_variant=3)
    future.set_result(plan)
    assert poll() is not None
    return e, b, clock, state, poll, obs, original


def arrive(case):
    e, b, clock, state, poll, _, _ = case
    assert e.active_grasp_selection is None and not e.approach_arrivals
    b.after_publication(publication_id='first', measured16=state['measured16'],
                        publication_complete=True, now_ns=clock[0])
    assert not e.approach_arrivals
    state['full22'][7] = state['measured16'][7] = state['held16'][7] = .1
    for delta in (20_000_000, 100_000_000):
        clock[0] += delta
        assert poll() is not None
        b.after_publication(publication_id=str(clock[0]), measured16=state['measured16'],
                            publication_complete=True, now_ns=clock[0])
    assert b.proposal is None and b.future_kind == 'verify'
    assert e.active_grasp_selection is None and len(e.approach_arrivals) == 1


def verdict(case, outcome='success', reason=''):
    e, b, _, _, poll, _, _ = case
    b.future.set_result(SkillResult(request_id=e.running.request_id, lifecycle='completed',
        outcome=outcome, reason=reason, evidence_ids=('fresh-evidence',) if outcome=='success' else ()))
    assert poll() is None


def next_grasp(case):
    e, _, clock, _, _, obs, original = case
    current = fresh_observation(obs, clock[0])
    e.update_observation(current)
    intent = original.model_copy(update=dict(skill='grasp', request_id='grasp-next',
        observation_id=current.observation_id, scene_version=current.scene_version))
    e.submit_skill(intent.model_dump())
    assert e.take_pending() == intent
    return intent


def test_actual_arrival_and_verified_approach_preserve_descriptor_into_grasp(tmp_path):
    case = prepared(tmp_path); e, b, _, _, _, obs, original = case
    try:
        arrive(case)
        verdict(case)
        selected = e.grasp_selection_for(next_grasp(case))
        assert selected['descriptor'] == DESCRIPTOR and selected['candidate_variant'] == 3
        assert selected['reference_object_pose'] == list(POSE)
        assert selected['reference_object_frame_id'] == 'right_arm_native'
        assert selected['source_observation_id'] == obs.observation_id
        assert selected['source_request_id'] == selected['completion_request_id'] == original.request_id
        assert selected['verified_evidence_ids'] == ['fresh-evidence']
        selected['descriptor']['pitch_offset_deg'] = -30
        assert e.active_grasp_selection['descriptor'] == DESCRIPTOR
        rows = [json.loads(line) for line in (e.store.root/'trace.jsonl').read_text().splitlines()]
        kinds = [r['kind'] for r in rows]
        assert kinds.index('harness_measured_waypoint') < kinds.index('harness_approach_grasp_arrived')
        assert kinds.index('harness_approach_grasp_arrived') < kinds.index('harness_grasp_selection_committed')
    finally: b.close()


@pytest.mark.parametrize('cancel', [False, True])
def test_pending_then_legal_recheck_uses_original_arrived_plan_and_never_replays(tmp_path, cancel):
    case = prepared(tmp_path); e, b, clock, _, poll, obs, original = case
    try:
        arrive(case)
        for _ in range(2): verdict(case, 'pending_review', 'fresh_post_motion_scene_missing')
        result = e.results[original.request_id]
        assert result.verification_request_id == original.request_id
        assert not e.active_grasp_selection
        publication_count = e.request_publications[original.request_id]['publication_count']
        clock[0] += 20_000_000
        current = fresh_observation(obs, clock[0]); e.update_observation(current)
        check = original.model_copy(update=dict(request_id='check-only', verification_request_id=original.request_id,
            observation_id=current.observation_id, scene_version=current.scene_version))
        e.submit_skill(check.model_dump()); assert poll() is None
        future = b.future
        if cancel:
            future.set_running_or_notify_cancel()
            b.revoke('manual_takeover')
        future.set_result(SkillResult(request_id=check.request_id, lifecycle='completed', outcome='success',
                                      evidence_ids=('later-fresh-evidence',)))
        assert poll() is None
        assert e.request_publications[original.request_id]['publication_count'] == publication_count
        assert 'check-only' not in e.request_publications
        if cancel:
            assert e.active_grasp_selection is None and not e.approach_arrivals
        else:
            selected = e.grasp_selection_for(next_grasp(case))
            assert selected['source_request_id'] == original.request_id
            assert selected['completion_request_id'] == 'check-only'
            assert selected['reference_object_pose'] == list(POSE)
            assert selected['verified_evidence_ids'] == ['later-fresh-evidence']
    finally: b.close()


@pytest.mark.parametrize('outcome', ['failure', 'interrupted', 'pending_review'])
def test_unverified_arrival_does_not_promote_selection(tmp_path, outcome):
    case = prepared(tmp_path); e, b, *_ = case
    try:
        arrive(case)
        verdict(case, outcome, 'inconclusive_geometry')
        assert e.active_grasp_selection is None and not e.approach_arrivals
        assert 'approach_grasp' not in e.completed_skills
    finally: b.close()


@pytest.mark.parametrize('stage', ['candidate', 'first_publication'])
def test_a_candidate_or_partial_motion_is_not_an_arrived_selection(tmp_path, stage):
    case = prepared(tmp_path); e, b, clock, state, _, _, original = case
    try:
        if stage == 'first_publication':
            b.after_publication(publication_id='first', measured16=state['measured16'],
                publication_complete=True, now_ns=clock[0])
        assert not e.approach_arrivals
        # Even a verifier success alone cannot manufacture missing arrival lineage.
        e.complete(original.request_id, SkillResult(request_id=original.request_id,
            lifecycle='completed', outcome='success', evidence_ids=('verifier-alone',)))
        assert e.active_grasp_selection is None
    finally: b.close()


@pytest.mark.parametrize('field', ['session_id','owner_instance_id','task_hash','epoch','calibration_id'])
def test_owner_context_cannot_cross_request_scope(tmp_path, field):
    case = prepared(tmp_path); e, b, *_ = case
    try:
        arrive(case); verdict(case)
        intent = next_grasp(case)
        assert e.grasp_selection_for(intent)
        value = 999 if field == 'epoch' else 'other'
        assert e.grasp_selection_for(intent.model_copy(update={field: value})) is None
        assert e.grasp_selection_for(intent.model_copy(update={'skill': 'regrasp'})) is None
    finally: b.close()


@pytest.mark.parametrize('transition', ['revoke','prepare_task','regrasp'])
def test_task_lifecycle_clears_committed_and_pending_context(tmp_path, transition):
    case = prepared(tmp_path); e, b, _, _, _, _, original = case
    try:
        arrive(case); verdict(case)
        assert e.active_grasp_selection
        if transition == 'revoke': e.revoke('manual_takeover')
        elif transition == 'prepare_task':
            # prepare_task's own reset must work independently of revoke's reset.
            e.authorized = False
            e.prepare_task(e.session, e.task.model_dump())
        else:
            e.submit_skill(original.model_copy(update={'skill':'regrasp','request_id':'regrasp-new'}).model_dump())
            e.take_pending()
        assert e.active_grasp_selection is None and not e.approach_arrivals
    finally: b.close()


def test_arrival_method_is_not_rpc_and_cannot_record_unpublished_request(tmp_path):
    from f1_harness.gateway import OWNER_METHODS
    case = prepared(tmp_path); e, b, clock, _, _, obs, original = case
    try:
        assert not {'record_approach_arrival','grasp_selection_for'} & set(OWNER_METHODS)
        with pytest.raises(HarnessError, match='approach_arrival_scope_mismatch'):
            e.record_approach_arrival(request_id=original.request_id, descriptor=DESCRIPTOR,
                candidate_variant=3, observation=obs, measured_at_ns=clock[0])
        assert not e.approach_arrivals
    finally: b.close()


def test_control_passes_owner_context_as_internal_planner_kwarg_without_changing_callback(tmp_path, monkeypatch):
    case = prepared(tmp_path/'owner'); e, b, _, state, _, obs, _ = case
    target = tuple(state['held16'])
    calls = []
    class Rpc:
        def __init__(self, *args, **kwargs): pass
        def call(self, method, **kwargs):
            calls.append((method, kwargs))
            intent = kwargs['intent']
            return asdict(JointProposal(intent['request_id'], intent['epoch'], intent['scene_version'],
                intent['calibration_id'], tuple(state['full22']), (target,), .1, 100, .5))
    monkeypatch.setattr('f1_harness.control.UnixClient', Rpc)
    monkeypatch.setattr('f1_harness.control.OwnerBridge.serve', lambda self, path: None)
    monkeypatch.setattr('f1_harness.revalidation.require_planning_geometry', lambda *args, **kwargs: None)
    source = Path(__file__).parents[1]/'configs/owner.example.json'
    path = tmp_path/'owner-config.json'; path.write_text(source.read_text())
    bridge = build_bridge(path, tmp_path/'composition', powered=False)
    try:
        arrive(case); verdict(case); intent = next_grasp(case)
        selected = e.grasp_selection_for(intent)
        bridge.engine.grasp_selection_for = lambda candidate: selected if candidate == intent else None
        proposal = bridge.planner(intent, obs, state['full22'], state['held16'])
        assert proposal.request_id == intent.request_id
        assert calls[0][0] == 'plan_skill'
        assert calls[0][1]['grasp_selection'] == selected
        assert 'grasp_selection' not in calls[0][1]['intent']
    finally:
        b.close(); bridge.close(); bridge.journal.close()

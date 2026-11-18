"""Preview reuse retains current anchors and rechecks changed loaded geometry.

Fast contract tests use deterministic numerics. The optional recorded-key cases
read the actual V32 input pairs; native FK, owner output and dense sweeps are
recorded separately by artifacts/latency-repair-20261117/replay_preview.py.
"""
import json
from pathlib import Path

import numpy as np
import pytest

from f1_harness.contracts import HarnessError, Observation, SkillIntent, Task, digest
from f1_harness.owner_bridge import JointProposal
from f1_harness.revalidation import (
    RevalidationEvidenceUpdated,
    preview_submission_key,
    proof_observation,
)
from test_full_development_skills import pose
from test_preview_submission import fresh, world_case


@pytest.mark.parametrize('name', ['loaded-world', 'loaded-transfer'])
def test_recorded_pairs_match_semantic_key_despite_distinct_physical_inputs(name):
    source = Path(__file__).resolve().parents[1] / 'artifacts/latency-repair-20261117/preview-inputs.json'
    if not source.is_file():
        pytest.skip('Recorded V32 replay inputs are not installed')
    inputs = json.loads(source.read_text())
    case = next(c for c in inputs['pairs'] if c['case_id'] == name)
    task = Task.model_validate(inputs['task'])
    a, b = (case[k] for k in ('preview', 'submit'))
    assert a['intent']['parameters'] == b['intent']['parameters']
    assert not np.array_equal(a['full_reference'], b['full_reference'])
    assert .8 < np.max(np.abs(np.asarray(a['held_reference']) - b['held_reference'])) < .9
    keys = [preview_submission_key(task, inputs['fixture'],
        SkillIntent.model_validate(e['intent']), Observation.model_validate(e['observation'])) for e in (a, b)]
    assert keys[0] is not None and keys[0] == keys[1]


def loaded_preview():
    service, obs, intent, clock, goal = world_case()
    state = list(obs.state16)
    state[15] = 100.
    obj = obs.objects[0].model_copy(update=dict(held=True, pose=tuple(pose(0, 0, .4).ravel()),
        eef_object_pose=tuple(pose(0, 0, -.1).ravel())))
    obs = obs.model_copy(update=dict(objects=(obj, *obs.objects[1:]), state16=tuple(state)))
    args = dict(intent=intent.model_dump(), observation=obs.model_dump(),
        full22=[0.] * 22, held16=list(obs.state16))
    candidate = service.dispatch('plan_skill', dict(args, preview=True))
    return service, obs, intent, clock, goal, candidate


def submitted_loaded_preview():
    service, obs, intent, clock, goal, preview = loaded_preview()
    latest = fresh(obs, clock)
    request = intent.model_copy(update=dict(request_id='submit', observation_id=latest.observation_id,
        scene_version=latest.scene_version))
    held = list(obs.state16)
    held[7] += .83  # Measured preview anchor and actual owner's held command differ.
    held[0] += .002
    full = [0.] * 22
    full[7] += .002
    count = service.numerics.calls.count('solve_path')
    submitted = service.dispatch('plan_skill', dict(intent=request.model_dump(),
        observation=latest.model_dump(), full22=full, held16=held))
    assert service.numerics.calls.count('solve_path') == count
    assert np.array_equal(np.asarray(preview['targets'])[:, 7:14], np.asarray(submitted['targets'])[:, 7:14])
    assert np.array_equal(submitted['trajectory']['anchor'], held)
    assert np.array_equal(submitted['full_reference'], full)
    assert np.all(np.asarray(submitted['targets'])[:, :7] == np.asarray(held)[:7])
    return service, latest, request, clock, goal, submitted, held, full


def changed_attachment(obs, clock, delta):
    newer = fresh(obs, clock)
    attachment = np.array(newer.objects[0].eef_object_pose).reshape(4, 4)
    attachment[0, 3] += delta
    obj = newer.objects[0].model_copy(update={'eef_object_pose': tuple(attachment.ravel())})
    return newer.model_copy(update={'objects': (obj, *newer.objects[1:])})


def test_reused_loaded_path_consumes_updated_attachment_and_rejects_new_obstacle(monkeypatch):
    service, original, intent, clock, goal, candidate, held, full = submitted_loaded_preview()
    latest = changed_attachment(original, clock, .02)
    args = dict(intent=intent.model_dump(), proposal=candidate, original_observation=original.model_dump(),
        observation=latest.model_dump(), full22=full, held16=held, after_ns=clock[0] - 1)
    import f1_harness.revalidation as revalidation
    actual = revalidation._motion_clear
    attachments = []
    def observe(scene, before, after, attached, **kwargs):
        attachments.append(None if attached is None else np.asarray(attached).copy())
        return actual(scene, before, after, attached, **kwargs)
    monkeypatch.setattr(revalidation, '_motion_clear', observe)
    native_calls = []
    call = service.numerics.call
    def capture(method, **kwargs):
        if method == 'solve_path':
            native_calls.append(kwargs)
            assert 'joint_path_to_validate' in kwargs['scene']
        return call(method, **kwargs)
    monkeypatch.setattr(service.numerics, 'call', capture)
    proof = service.dispatch('revalidate_plan', args)
    assert len(native_calls) == len(candidate['geometry_trace'])
    assert proof['geometry_checks']['native_fixed_path_stages'] == len(native_calls)
    for request, entry in zip(native_calls, candidate['geometry_trace']):
        assert np.array_equal(request['scene']['joint_path_to_validate'],
            candidate['targets'][entry['start_index']:entry['end_index']])
        assert np.allclose(request['scene']['eef_object'],
            np.array(latest.objects[0].eef_object_pose).reshape(4, 4))
    assert proof['geometry_checks']['swept_segments'] == len(candidate['targets'])
    assert proof['geometry_checks']['swept_observation_sha256'] == digest(latest)
    assert attachments and all(np.allclose(a, np.array(latest.objects[0].eef_object_pose).reshape(4, 4)) for a in attachments)
    # This is a deliberate test obstacle, not a modified recorded scene.
    service.fixture['obstacles'] = (*service.fixture['obstacles'],
        dict(role='new_blocker', min=[-.03] * 3, max=[.03] * 3, pose=goal.tolist()))
    with pytest.raises(HarnessError, match='sweep'):
        service.dispatch('revalidate_plan', args)


def test_later_attachment_estimate_requests_resweep_but_attachment_loss_rejects():
    service, original, intent, clock, _, candidate, held, full = submitted_loaded_preview()
    latest = fresh(original, clock)
    args = dict(intent=intent.model_dump(), proposal=candidate, original_observation=original.model_dump(),
        observation=latest.model_dump(), full22=full, held16=held, after_ns=clock[0] - 1)
    proof = service.dispatch('revalidate_plan', args)
    live = changed_attachment(latest, clock, .02)
    with pytest.raises(RevalidationEvidenceUpdated, match='attachment_pose_changed'):
        proof_observation(proof, proposal=JointProposal(**candidate), original=original, intent=intent,
            task=service.task, now_ns=clock[0], max_age_ns=500_000_000, live_observation=live)
    missing = live.model_copy(update={'objects': (
        live.objects[0].model_copy(update={'eef_object_pose': None}), *live.objects[1:])})
    with pytest.raises(HarnessError, match='attachment_changed'):
        service.dispatch('revalidate_plan', dict(args, observation=missing.model_dump()))

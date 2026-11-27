"""H-51 item 1: registered grasp pitch priority ahead of nominal fallback (even with geometric ranking)."""
from dataclasses import replace

from f1_harness.candidate_ranking import rank_grasp_choices
from f1_harness.contracts import HarnessError
from f1_harness.grasp_candidates import descriptors
from test_grasp_selection_planning import setup


def test_prefer_registered_grasp_pitch_orders_registered_first_and_keeps_fallback(monkeypatch):
    plan, obs, intent, _ = setup()
    plan.fixture['grasp_pitch_offsets_deg'] = [15.0]
    plan.fixture['prefer_registered_grasp_pitch'] = True
    plan.fixture['geometric_candidate_ranking'] = True
    plan.fixture['candidate_order'] = {'approach_grasp': [3, 0, 1, 2]}
    # Add a posture prior matching nominal 0 deg so geometric ranking without tier preservation
    # would otherwise pull 0 deg ahead of 15 deg.
    scene0 = plan._scene(obs, [0.] * 22, obs.state16)
    assert descriptors(scene0) == (
        dict(strategy='tcp_pitch', pitch_offset_deg=15.0),
        dict(strategy='tcp_pitch', pitch_offset_deg=0.0),
    )
    plan.posture_prior = {'eef_poses': {'grasp': (scene0.base_object @ scene0.object_grasp_eef).tolist()}}

    tried = []

    def fail_pitched_succeed_fallback(intent, scene, held, full, variant, *, deadline_ns=None):
        pitch = scene.grasp_candidate_descriptor['pitch_offset_deg']
        tried.append((pitch, variant))
        if pitch == 15.0:
            raise HarnessError('scene_sweep_blocked')
        return {'geometry_trace': [{}]}

    monkeypatch.setattr(plan, '_solve_candidate', fail_pitched_succeed_fallback)
    plan.dispatch('plan_skill', dict(
        intent=intent.model_dump(), observation=obs.model_dump(),
        full22=[0.] * 22, held16=list(obs.state16),
    ))
    # All 4 variants of +15 deg must be attempted before falling back to 0 deg.
    assert [p for p, _ in tried[:4]] == [15.0, 15.0, 15.0, 15.0]
    assert tried[4][0] == 0.0


def test_prefer_registered_grasp_pitch_succeeds_on_registered_pitch_first(monkeypatch):
    plan, obs, intent, _ = setup()
    plan.fixture['grasp_pitch_offsets_deg'] = [15.0]
    plan.fixture['prefer_registered_grasp_pitch'] = True
    plan.fixture['geometric_candidate_ranking'] = True
    scene0 = plan._scene(obs, [0.] * 22, obs.state16)
    plan.posture_prior = {'eef_poses': {'grasp': (scene0.base_object @ scene0.object_grasp_eef).tolist()}}

    tried = []

    def succeed_first(intent, scene, held, full, variant, *, deadline_ns=None):
        pitch = scene.grasp_candidate_descriptor['pitch_offset_deg']
        tried.append((pitch, variant))
        return {'geometry_trace': [{}]}

    monkeypatch.setattr(plan, '_solve_candidate', succeed_first)
    plan.dispatch('plan_skill', dict(
        intent=intent.model_dump(), observation=obs.model_dump(),
        full22=[0.] * 22, held16=list(obs.state16),
    ))
    assert len(tried) == 1 and tried[0][0] == 15.0


def test_rank_grasp_choices_preserves_descriptor_tier_only_when_requested():
    plan, obs, _, _ = setup()
    plan.fixture['grasp_pitch_offsets_deg'] = [15.0]
    plan.fixture['prefer_registered_grasp_pitch'] = True
    scene0 = plan._scene(obs, [0.] * 22, obs.state16)
    prior = {'eef_poses': {'grasp': (scene0.base_object @ scene0.object_grasp_eef).tolist()}}
    cands = [(d, v) for d in descriptors(scene0) for v in range(4)]
    ordered_tiered, _ = rank_grasp_choices(scene0, cands, prior, preserve_descriptor_order=True)
    assert [d['pitch_offset_deg'] for d, _ in ordered_tiered[:4]] == [15.0] * 4
    assert [d['pitch_offset_deg'] for d, _ in ordered_tiered[4:]] == [0.0] * 4
    # Without prefer_registered_grasp_pitch, descriptors still keep 0.0 first.
    legacy_scene = replace(scene0, prefer_registered_grasp_pitch=False)
    assert descriptors(legacy_scene)[0]['pitch_offset_deg'] == 0.0

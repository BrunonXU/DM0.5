"""H-53 & H-48: release -> withdraw_reset -> verify_placed state machine, static return anchor,
_solved gating, verifier compatibility, lid crop export, and funnel pending-confirmation bucket."""
from collections import deque
from pathlib import Path
from types import SimpleNamespace
import json
import numpy as np
import pytest

from f1_harness.adapters import ScopedBackend
from f1_harness.contracts import (
    FixturePose, Frame, Mode, Observation, Pairing, SceneObject, SkillIntent, SkillResult, Task, digest,
)
from f1_harness.engine import TaskEngine
from f1_harness.evidence import EvidenceStore
from f1_harness.observation import require_geometry_identity
from f1_harness.program_verifier import verify_program
from f1_harness.recovery_programs import compile_library
from f1_harness.return_observation import observe_return
from f1_harness.scene_pipeline import CACHED_APERTURE_MAX_AGE_NS, CalibratedScenePipeline
from f1_harness.verification import verify as verify_postcondition


def _task(with_withdraw=True):
    skills = ('approach_grasp', 'grasp', 'verify_grasp', 'inspect_in_hand',
              'transfer_above_hole', 'align', 'insert', 'release', 'verify_placed')
    if with_withdraw:
        skills = skills + ('withdraw_reset',)
    return Task(
        task_id='t-h53', shape='square', object_id='square-1', hole_id='square-hole-1',
        calibration_id='cal-h53', goal='placed', admission_profile='development_coarse',
        allowed_skills=skills,
    )


def _frame(camera, stream, now_ns, seq, sha):
    return Frame(
        camera=camera, stream=stream, frame_id=f'{camera}_{stream}', sequence=seq,
        source_ns=now_ns - 5_000_000, received_ns=now_ns, clock_domain='test',
        width=640, height=480, encoding='rgb8' if stream == 'rgb' else '16UC1',
        blob_sha256=sha,
    )


def _obs(now_ns, *, objects=(), scope='coarse_static_task', camera='head', seq=1, gripper=0.0):
    frames = (_frame(camera, 'rgb', now_ns, seq, 'a' * 64),
              _frame(camera, 'depth', now_ns, seq, 'b' * 64))
    pairings = (Pairing(camera=camera, rgb_sequence=seq, depth_sequence=seq,
                        skew_ns=0, quality='source_time_bounded'),)
    state16 = tuple([0.0] * 15 + [float(gripper)])
    return Observation(
        observation_id=f'obs-h53-{seq}', acquisition_instance='acq-1', clock_domain='test',
        received_ns=now_ns, state_received_ns=now_ns,
        state16=state16, state_valid_mask=(True,) * 16,
        frames=frames, pairings=pairings, objects=tuple(objects),
        stationary=True, calibration_id='cal-h53',
        geometry_scope=scope, geometry_camera=camera,
        scene_version=f'sv-{seq}',
    )


def _run_place_program(*, withdraw=True, withdraw_outcome='success', verify_outcome='success'):
    calls = []

    class FakeRobo:
        def skill(self, name, **params):
            calls.append(name)
            if name == 'withdraw_reset':
                return dict(outcome=withdraw_outcome, reason='ok' if withdraw_outcome == 'success' else 'return_blocked')
            if name == 'verify_placed':
                return dict(outcome=verify_outcome, reason='placement_observed' if verify_outcome == 'success' else 'object_not_observed')
            return dict(outcome='success', reason='ok')

        def inspect_plan(self, name, **params):
            return dict(planned=True)

        def observe(self):
            return dict(status={})

    ns = {'robo': FakeRobo()}
    exec(compile_library(local_corrections=0, regrasp_limit=0), ns)
    result = ns['place_held_block'](withdraw=withdraw, start='release')
    return calls, result


def test_h53_place_held_block_order_and_withdraw_failure_short_circuit():
    # withdraw=True -> release -> withdraw_reset -> verify_placed
    calls, res = _run_place_program(withdraw=True)
    assert calls == ['release', 'withdraw_reset', 'verify_placed']
    assert res['outcome'] == 'success' and res['program_stage'] == 'verify_placed'

    # withdraw=False -> keeps release -> verify_placed
    calls_no, res_no = _run_place_program(withdraw=False)
    assert calls_no == ['release', 'verify_placed']
    assert res_no['outcome'] == 'success' and res_no['program_stage'] == 'verify_placed'

    # withdraw_reset failure -> stops without verify_placed
    calls_fail, res_fail = _run_place_program(withdraw=True, withdraw_outcome='failure')
    assert calls_fail == ['release', 'withdraw_reset']
    assert res_fail['outcome'] == 'failure' and res_fail['program_stage'] == 'withdraw_reset'


def test_h53_engine_admits_withdraw_after_release_and_toggles_return_scene_requested(tmp_path):
    now = 50_000_000_000
    eng = TaskEngine(EvidenceStore(tmp_path / 'store'), mode=Mode.FAKE, clock=lambda: now)
    sess = eng.open_session()['session_id']
    task = _task(with_withdraw=True)
    eng.prepare_task(sess, task.model_dump())
    eng.local_start(physical_event=True, feedback_healthy=True)

    # Before release: return_scene_requested is False
    assert eng.status()['perception_context']['return_scene_requested'] is False

    # Complete through release (without verify_placed)
    eng.completed_skills.update(('approach_grasp', 'grasp', 'verify_grasp', 'inspect_in_hand',
                                 'transfer_above_hole', 'align', 'insert', 'release'))
    ctx = eng.status()['perception_context']
    assert ctx['return_scene_requested'] is True
    assert ctx['placement_verified'] is False

    # TaskEngine admits withdraw_reset right after release
    hole_ret = SceneObject(
        object_id='square-hole-1', shape='square', pose=tuple(np.eye(4).ravel()),
        frame_id='base', planning_margin_m=0.015, evidence_ids=('a' * 64, 'b' * 64),
        visible=False, stationary=True, source='static_return_anchor', measured_at_ns=now - 5_000_000_000,
    )
    obs_ret = _obs(now, objects=(hole_ret,), scope='coarse_fixture_return', camera='head', seq=1)
    eng.update_observation(obs_ret)
    intent_wr = SkillIntent(
        request_id='req-wr', session_id=sess, owner_instance_id=eng.instance,
        task_id=task.task_id, task_hash=digest(task),
        observation_id=obs_ret.observation_id, epoch=eng.epoch,
        scene_version=obs_ret.scene_version, calibration_id=task.calibration_id,
        skill='withdraw_reset', parameters={}, budget_ms=10_000,
    )
    eng._check_intent(intent_wr)  # Must NOT raise skill_precondition:verify_placed

    # After withdraw_reset completes, return_scene_requested returns to False so
    # scene_pipeline switches back to coarse_static_task for verify_placed.
    eng.completed_skills.add('withdraw_reset')
    ctx_after = eng.status()['perception_context']
    assert ctx_after['return_scene_requested'] is False
    assert ctx_after['placement_verified'] is False


def _stub_pipeline(now_ns):
    pipe = object.__new__(CalibratedScenePipeline)
    pipe.config = dict(
        camera='right',
        hole_camera=dict(camera='head'),
        hole_id='square-hole-1',
        native_frame='base',
        planning_margin_m=0.015,
        calibration_id='cal-h53',
        stationary_window_ns=300_000_000,
        stationary_delta_deg=0.5,
        gripper_stationary_delta=1.0,
    )
    pipe.coarse = True
    pipe.development = False
    pipe.context_identity = ('o1', 's1', 't-h53', 'h', 1, 1)
    pipe.acquisition_identity = ('acq-1', 'test', None, 'cal-h53', False)
    pipe.return_stamps = None
    pipe.return_history = deque(maxlen=16)
    pipe.revision = 0
    pipe.last_timing = {}
    pipe.measured_fixture = None
    pipe.return_static_anchor = None
    pipe._gripper_is_open = lambda obs, frame_ns=None: obs.state16[15] <= 5.0
    pipe._gripper_value = lambda obs, frame_ns=None: obs.state16[15]
    pipe._secondary_view = lambda obs, cam: (_ for _ in ()).throw(ValueError('partial_lid_landmarks_unavailable'))
    pipe._cross_view_aperture = lambda obs, depth: None
    return pipe


def test_h53_occluded_return_uses_static_anchor_and_enforces_45s_fault_and_no_verify_placed():
    base_ns = 100_000_000_000
    pipe = _stub_pipeline(base_ns)
    ctx_ok = dict(authorized=True, placement_verified=False, return_scene_requested=True)
    pipe._update_return_static_anchor(
        hole_pose=np.eye(4), evidence_ids=('1' * 64, '2' * 64),
        measured_at_ns=base_ns, source_kind='pre_transport_aperture', context=ctx_ok,
    )

    # 1) Occluded head at age=10s -> emits static_return_anchor for withdraw_reset
    obs_in = _obs(base_ns + 10_000_000_000, objects=(), scope='coarse_static_task', camera='head', seq=1)
    out = observe_return(pipe, obs_in, ctx_ok)
    assert out.geometry_scope == 'coarse_fixture_return'
    assert out.objects[0].source == 'static_return_anchor'
    assert out.objects[0].visible is False
    assert out.objects[0].measured_at_ns == base_ns
    assert 'return_fixture_from_static_anchor' in out.quality_warnings
    assert pipe.last_timing['return_static_anchor']['status'] == 'used'
    assert pipe.last_timing['return_static_anchor']['age_s'] == 10.0

    # 2) static_return_anchor MUST NOT enter verify_placed (both observation.py & verification.py reject!)
    task = _task()
    cube = SceneObject(
        object_id='square-1', shape='square', pose=tuple(np.eye(4).ravel()),
        frame_id='base', planning_margin_m=0.015, visible=True, held=False,
        inserted=True, inserted_hole_id='square-hole-1', stationary=True,
        evidence_ids=('a' * 64, 'b' * 64),
    )
    bad_vp = _obs(base_ns + 11_000_000_000, objects=(cube, out.objects[0]),
                  scope='coarse_static_task', camera='head', seq=2)
    with pytest.raises(ValueError, match='static_return_anchor_only_allowed_for_withdraw_reset'):
        require_geometry_identity(bad_vp, goal=task.goal, admission_profile=task.admission_profile,
                                  skill='verify_placed', hole_id=task.hole_id)
    vp_res = verify_postcondition('verify_placed', task, bad_vp, 'req-vp')
    assert vp_res.outcome == 'pending_review' and vp_res.reason == 'matching_hole_evidence_missing'

    # 3) Contact fault since anchor -> withdraw_reset refuses return scene
    ctx_fault = dict(ctx_ok, contact_fault=dict(at_ns=base_ns + 5_000_000_000, reason='motion_stalled'))
    obs_f = _obs(base_ns + 12_000_000_000, objects=(), scope='coarse_static_task', camera='head', seq=3)
    with pytest.raises(ValueError, match='return_anchor_invalidated:motion_stalled'):
        observe_return(pipe, obs_f, ctx_fault)

    # 4) Age > 45s -> withdraw_reset refuses return scene
    obs_old = _obs(base_ns + CACHED_APERTURE_MAX_AGE_NS + 1_000_000_000, objects=(),
                   scope='coarse_static_task', camera='head', seq=4)
    with pytest.raises(ValueError, match='return_anchor_age_exceeded'):
        observe_return(pipe, obs_old, ctx_ok)


def test_h53_solved_requires_both_withdraw_and_verify_placed(tmp_path):
    clock = [30_000_000_000]
    task = _task(with_withdraw=True)
    eng = TaskEngine(EvidenceStore(tmp_path / 'store'), mode=Mode.FAKE, clock=lambda: clock[0])
    sess = eng.open_session()['session_id']
    eng.prepare_task(sess, task.model_dump())
    eng.local_start(physical_event=True, feedback_healthy=True)
    eng.completed_skills.update(('approach_grasp', 'grasp', 'verify_grasp', 'inspect_in_hand',
                                 'transfer_above_hole', 'align', 'insert', 'release'))

    seq = [0]

    class FakeObs:
        def observe(self):
            seq[0] += 1
            clock[0] += 1_000_000
            return _obs(clock[0], seq=seq[0])

    outcomes = iter([
        dict(outcome='success', reason='owner_return_arrival_and_fresh_fixture', evidence_ids=('a' * 64,)),
        dict(outcome='pending_review', reason='object_not_observed', evidence_ids=()),
        dict(outcome='success', reason='placement_observed', evidence_ids=('a' * 64,)),
    ])

    class FakePlant:
        def advance(self):
            req = eng.take_pending()
            assert req is not None
            spec = next(outcomes)
            eng.complete(req.request_id, SkillResult(
                request_id=req.request_id, lifecycle='completed',
                outcome=spec['outcome'], reason=spec['reason'],
                evidence_ids=spec['evidence_ids'], synthetic=True, assisted=False,
            ))

    backend = ScopedBackend(eng, FakeObs(), session_id=sess, plant=FakePlant(), poll_s=0.001)
    # 1) withdraw_reset succeeds alone -> NOT solved yet!
    r_wr = backend.execute_tool('execute_skill', {'name': 'withdraw_reset'})
    assert r_wr['outcome'] == 'success'
    assert backend.solved() is False
    # 2) verify_placed returns pending_review (object_not_observed) -> STILL NOT solved!
    r_vp1 = backend.execute_tool('execute_skill', {'name': 'verify_placed'})
    assert r_vp1['outcome'] == 'pending_review'
    assert backend.solved() is False
    # 3) verify_placed succeeds -> BOTH done -> solved is True!
    r_vp2 = backend.execute_tool('execute_skill', {'name': 'verify_placed'})
    assert r_vp2['outcome'] == 'success'
    assert backend.solved() is True


def test_h53_program_verifier_accepts_both_withdraw_orders():
    from f1_harness.recovery_programs import registered_program
    scope = dict(owner_instance_id='o1', session_id='s1', task_id='t1',
                 task_hash='th1', epoch=1, calibration_id='cal1')
    prog = registered_program('insert_reobserve_realign')
    candidate = dict(program=prog, skills=list(prog['skills']))
    for order in (('withdraw_reset', 'verify_placed'), ('verify_placed', 'withdraw_reset')):
        rows = [
            dict(kind='task_prepared', task_hash='th1',
                 task=dict(allowed_skills=['insert', 'retreat', 'reobserve', 'align', 'release', 'withdraw_reset', 'verify_placed'])),
            dict(kind='skill_accepted', intent=dict(request_id='r-ins0', skill='insert', **scope)),
            dict(kind='skill_result', skill='insert', result=dict(request_id='r-ins0', outcome='failure', synthetic=False, assisted=False, evidence_ids=['e0'])),
            dict(kind='skill_accepted', intent=dict(request_id='r-ret', skill='retreat', **scope)),
            dict(kind='skill_result', skill='retreat', result=dict(request_id='r-ret', outcome='success', synthetic=False, assisted=False, evidence_ids=['e1'])),
            dict(kind='skill_accepted', intent=dict(request_id='r-reo', skill='reobserve', **scope)),
            dict(kind='skill_result', skill='reobserve', result=dict(request_id='r-reo', outcome='success', synthetic=False, assisted=False, evidence_ids=['e2'])),
            dict(kind='skill_accepted', intent=dict(request_id='r-al', skill='align', **scope)),
            dict(kind='skill_result', skill='align', result=dict(request_id='r-al', outcome='success', synthetic=False, assisted=False, evidence_ids=['e3'])),
            dict(kind='skill_accepted', intent=dict(request_id='r-ins1', skill='insert', **scope)),
            dict(kind='skill_result', skill='insert', result=dict(request_id='r-ins1', outcome='success', synthetic=False, assisted=False, evidence_ids=['e4'])),
        ]
        placement_row = None
        for idx, sk in enumerate(order):
            rid = f'r-{sk}'
            rows.append(dict(kind='skill_accepted', intent=dict(request_id=rid, skill=sk, **scope)))
            if sk == 'withdraw_reset':
                rows.append(dict(kind='harness_measured_waypoint', request_id=rid))
            res_row = dict(kind='skill_result', skill=sk, result=dict(
                request_id=rid, outcome='success', synthetic=False, assisted=False, evidence_ids=[f'ef{idx}']))
            rows.append(res_row)
            if sk == 'verify_placed':
                placement_row = res_row
        verified = verify_program(candidate, rows, placement_row, synthetic=False)
        assert verified is not None and verified['program_id'] == 'insert_reobserve_realign', f'failed for order={order}'


def test_h53_run_funnel_buckets_placement_pending_confirmation():
    import sys
    scripts_dir = str(Path(__file__).resolve().parents[1] / 'scripts')
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import run_funnel
    table = json.loads((Path(scripts_dir) / 'funnel_reasons.json').read_text(encoding='utf-8'))
    auth = dict(kind='web_authorization', monotonic_ns=1_000_000_000, synthetic=False)
    owner = [
        auth,
        dict(kind='harness_skill_first_publication', published_ns=2_000_000_000),
        dict(kind='skill_result', skill='insert', monotonic_ns=5_000_000_000,
             result=dict(request_id='r7', outcome='success', synthetic=False, assisted=False)),
        dict(kind='skill_result', skill='withdraw_reset', monotonic_ns=7_000_000_000,
             result=dict(request_id='r9', outcome='success', synthetic=False, assisted=False)),
        dict(kind='skill_result', skill='verify_placed', monotonic_ns=8_000_000_000,
             result=dict(request_id='r8', outcome='pending_review', reason='object_not_observed',
                         synthetic=False, assisted=False)),
        dict(kind='authority_revoked', monotonic_ns=9_000_000_000, reason='task_finished'),
    ]
    metrics = run_funnel.owner_metrics(owner, auth, [], table, ended=True)
    assert metrics['complete_success'] is False
    assert metrics['placement_pending_confirmation'] is True
    assert metrics['placement_pending_confirmation_label'] == '放置待确认'
    assert metrics['placement_pending_confirmation_reason'] == 'object_not_observed'
    assert metrics['stages']['S9'] is True
    assert metrics['stage_interval_s']['S9'] == 2.0


def test_h53_verify_placed_head_lid_crop_saved_under_300kb(tmp_path):
    import io
    from PIL import Image
    from f1_harness.remote import RemoteTaskBackend

    img = Image.fromarray(np.random.default_rng(0).integers(0, 255, (720, 1280, 3), dtype=np.uint8))
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    png_bytes = buf.getvalue()

    backend = object.__new__(RemoteTaskBackend)
    backend.evidence_store = EvidenceStore(tmp_path / 'ev')
    backend.call = lambda method, **kw: dict(bytes=png_bytes) if method == 'view_camera' else {}

    intent = SimpleNamespace(request_id='abcdef1234567890')
    backend._capture_verify_placed_lid_crop(intent, dict(outcome='pending_review', reason='object_not_observed'))
    crops = list((tmp_path / 'ev/verify_placed_crops').glob('*.jpg'))
    assert len(crops) == 1
    assert 0 < crops[0].stat().st_size <= 300_000
    saved = Image.open(crops[0])
    assert saved.size == (1229 - 307, 720 - 28)

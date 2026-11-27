#!/usr/bin/env python3
"""H-47: Zero-output full placement dry-run (align -> insert -> release -> withdraw_reset -> verify_placed)
from real transport-arrival states of V62 Trial 1-2 and V60 Trial 1-2 using real PlanService, VerifyService,
TaskEngine, CalibratedScenePipeline, F1Owner/Ruckig approved-output (MemoryTransport), and output sweep audit."""
import copy
import json
import math
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
import numpy as np

W = Path('/home/xza_100850113/Artifacts/WorkspaceReports/github-harness-v53')
H = Path('/home/xza_100850113/projects/HARNESS')
A = H / 'artifacts/cached-aperture-align-v62-20261127'
SITE = next((A / 'full-validation-pinned').rglob('plan.local.json')).parent

# Load harness/round9 source + V62 frozen control/RPent + HARNESS scripts (for output_sweeps)
sys.path.insert(0, str(W / 'HARNESS/src'))
sys.path.insert(0, str(W / 'HARNESS/scripts'))
sys.path.insert(0, str(A / 'coordinator-e9d6a8feb0f3/control/src'))
sys.path.insert(0, str(A / 'coordinator-e9d6a8feb0f3/upstream/RPent'))

cal = json.loads((SITE / 'coarse-calibration.json').read_text())
cal_boot_id = cal['boot_id']
import f1_harness.development as dev_mod
_RealPath = dev_mod.Path
class _BootShim:
    def read_text(self, *a, **kw): return cal_boot_id + '\n'
dev_mod.Path = lambda *a, **kw: _BootShim() if len(a) == 1 and str(a[0]) == '/proc/sys/kernel/random/boot_id' else _RealPath(*a, **kw)
dev_mod._check_code = lambda *a, **kw: None

from f1_harness.contracts import Mode, Observation, SceneObject, SkillIntent, SkillResult, Task, digest
from f1_harness.engine import TaskEngine
from f1_harness.evidence import EvidenceStore
from f1_harness.geometry import insertion_depth_reached, transform
from f1_harness.ipc import UnixServer
from f1_harness.numerical_service import NumericalDispatcher
from f1_harness.observation import require_geometry_identity
from f1_harness.return_observation import observe_return
from f1_harness.scene_pipeline import CalibratedScenePipeline
from f1_harness.skill_services import PlanService, VerifyService
from f1_harness.verification import verify as verify_postcondition
from f1dagger.robot_io import F1Owner, make_output
from output_sweeps import sweep_outputs


class MemoryTransport:
    """Zero-hardware-output transport identical to replay_full_owner.py."""
    rmw_gids = {k: 'simulation' for k in ('joint_command', 'left_gripper_command', 'right_gripper_command')}
    own_gids = {}
    def prepare(self, key): pass
    def assert_exclusive_cached(self, key): pass
    def publish(self, key, value): pass
    def release(self, key): pass


def patch_infra(v):
    pinned = H.parent / 'F1-4242'
    if isinstance(v, dict): return {k: patch_infra(val) for k, val in v.items()}
    if isinstance(v, list): return [patch_infra(val) for val in v]
    if isinstance(v, str) and '/startup-read-v2/infra' in v:
        return v.replace('/home/user/Bruno/DAgger-runtime/f1-toys-20261016/full-h20/startup-read-v2/infra', str(pinned.resolve()))
    return v


def stream_approved_output(targets, full22, start16, clock_ns, spec, numeric, *, skill, request_id, obs_dump):
    """Stream planned targets through real F1Owner/Ruckig MemoryTransport; returns (final16, rows, audit_info)."""
    full = np.array(full22, dtype=float)
    measured = np.array(start16, dtype=float)
    t_ns = [int(clock_ns)]
    source = SimpleNamespace(
        feedback=lambda: (full.copy(), measured.copy(), (t_ns[0],) * 16),
        state_valid_mask=(True,) * 16,
    )
    out = make_output(spec['gate']['config'])
    owner = F1Owner(MemoryTransport(), source, numeric.numerics.mapping, out, numeric.numerics.gate, powered=True)
    owner.preventive_boundary_recovery = True
    owner.initialize(t_ns[0] / 1e9)
    owner.prepare()

    rows = []
    dt_ns = 20_000_000  # 50 Hz real control tick
    try:
        for idx, tgt in enumerate(targets):
            goal = np.array(tgt, dtype=float)
            # Step until Ruckig converges within 0.15 deg & 0.5 gripper (or bounded 1200 ticks = 24s)
            for tick in range(1200):
                t_ns[0] += dt_ns
                full[7:14] = measured[7:14]
                owner.approve(goal, t_ns[0] / 1e9, enabled=(False, True))
                submitted_raw, _, ev = owner.publish(t_ns[0] / 1e9)
                submitted = np.array(submitted_raw, dtype=float)
                numeric.numerics.mapping.check_action(submitted)
                numeric.numerics.gate.check_preventive(submitted, full)
                rows.append(dict(
                    skill=skill,
                    request_id=request_id,
                    virtual_ns=t_ns[0],
                    reference16=owner.selected.tolist(),
                    measured16=measured.tolist(),
                    submitted16=submitted.tolist(),
                    output_evidence=ev,
                    new_motion_reference=(tick == 0),
                ))
                measured = submitted.copy()
                arm_err = float(np.max(np.abs(measured[7:14] - goal[7:14])))
                grip_err = abs(float(measured[15] - goal[15]))
                if arm_err <= 0.15 and grip_err <= 0.5:
                    break
    finally:
        owner.close()

    q = np.array([r['submitted16'] for r in rows], dtype=float)
    t = np.array([r['virtual_ns'] for r in rows], dtype=np.int64)
    speed = np.diff(q[:, :14], axis=0) / (np.diff(t)[:, None] / 1e9) if len(q) > 1 else np.zeros((1, 14))
    max_vel = float(np.max(np.abs(speed)))
    vel_limit = float(out.planner.max_velocity)
    audit = dict(
        samples=len(rows),
        virtual_duration_s=round((t_ns[0] - clock_ns) / 1e9, 3),
        complete_publications=all(r['output_evidence']['publication_complete'] for r in rows),
        immutable_left_arm=bool(np.all(np.abs(q[:, :7] - q[0, :7]) <= 1e-9)),
        sampled_max_speed_deg_s=round(max_vel, 3),
        profile_max_velocity_deg_s=vel_limit,
        sampled_speed_within_profile=bool(max_vel <= vel_limit + 1e-6),
        final_arm_err_deg=round(float(np.max(np.abs(measured[7:14] - np.array(targets[-1])[7:14]))), 4),
        final_gripper_err=round(abs(float(measured[15] - targets[-1][15])), 4),
    )
    audit['passed'] = bool(
        audit['complete_publications']
        and audit['immutable_left_arm']
        and audit['sampled_speed_within_profile']
    )
    return measured, t_ns[0], rows, audit


def make_fresh_obs(base_obs, *, seq, now_ns, state16, objects, fixture, geometry_scope='coarse_static_task',
                   geometry_camera=None, quality_warnings=()):
    frames = tuple(
        f.model_copy(update=dict(
            received_ns=now_ns,
            source_ns=f.source_ns + (now_ns - base_obs.received_ns),
            sequence=f.sequence + seq,
        ))
        for f in base_obs.frames
        if geometry_camera is None or f.camera == geometry_camera
    )
    pairings = tuple(
        p.model_copy(update=dict(
            rgb_sequence=p.rgb_sequence + seq,
            depth_sequence=p.depth_sequence + seq,
        ))
        for p in base_obs.pairings
        if geometry_camera is None or p.camera == geometry_camera
    )
    return base_obs.model_copy(update=dict(
        observation_id=f'h47-sim:{seq}',
        received_ns=now_ns,
        state_received_ns=now_ns,
        state16=tuple(float(x) for x in state16),
        frames=frames,
        pairings=pairings,
        objects=tuple(objects),
        fixture=fixture,
        stationary=True,
        geometry_scope=geometry_scope,
        geometry_camera=geometry_camera or base_obs.geometry_camera,
        quality_warnings=tuple(quality_warnings),
        scene_version=digest(dict(
            seq=seq, scope=geometry_scope,
            objects=[o.model_dump() for o in objects],
            fixture=None if fixture is None else fixture.model_dump(),
        )),
    ))


def main():
    plan_cfg = json.loads((SITE / 'plan.local.json').read_text())
    num_cfg = json.loads((SITE / 'numerics.local.json').read_text())
    gw_cfg = json.loads((SITE / 'gateway.local.json').read_text())
    scene_cfg = gw_cfg['scene']['geometry']
    spec = patch_infra(copy.deepcopy(num_cfg['spec']))
    fixture_cfg = copy.deepcopy(plan_cfg['fixture'])
    task = Task.model_validate(plan_cfg['task'])

    tmp_dir = Path(tempfile.mkdtemp(prefix='h47-dryrun-'))
    spec['gate']['runtime'] = str(tmp_dir / 'numerics-gate')
    dispatcher = NumericalDispatcher(spec)
    assert dispatcher.numerics.max_wall_s == 15.0
    sock_path = tmp_dir / 'numerics.sock'
    server = UnixServer(sock_path, dispatcher.dispatch).start()

    trials = [
        ('V62-Trial1', W / 'Claude-advice/HARNESS/evidence/20261127-v62-batch/runs/trial1/owner-trace.jsonl', 114, 154),
        ('V62-Trial2', W / 'Claude-advice/HARNESS/evidence/20261127-v62-batch/runs/trial2/owner-trace.jsonl', 83, 102),
        ('V60-Trial1', W / 'Claude-advice/HARNESS/evidence/20261127-v60-batch/runs/trial1/owner-trace.jsonl', 77, 110),
        ('V60-Trial2', W / 'Claude-advice/HARNESS/evidence/20261127-v60-batch/runs/trial2/owner-trace.jsonl', 77, 101),
    ]

    report = {
        'schema': 'h47-placement-dry-run-v1',
        'physical_motion': False,
        'simulated_execution_feedback': True,
        'note': 'Execution feedback (block moves rigidly with hand; post-withdrawal head view sees inserted block) is simulated; verify_placed proves pipeline connectivity only, NOT physical placement success.',
        'ik_max_wall_s': dispatcher.numerics.max_wall_s,
        'trials': {},
    }

    try:
        for label, trace_path, pre_seq, arr_seq in trials:
            print('=' * 85, flush=True)
            print(f'H-47 TRIAL: {label} ({pre_seq} -> {arr_seq})', flush=True)
            by_seq = {}
            close_id = close_ns = last_full = None
            for line in trace_path.open():
                rec = json.loads(line)
                if rec.get('kind') == 'harness_grasp_close_published':
                    close_id, close_ns = rec.get('request_id'), rec.get('monotonic_ns')
                for k in ('full_reference', 'full22'):
                    if isinstance(rec.get(k), list) and len(rec[k]) == 22:
                        last_full = rec[k]
                ob = rec.get('observation')
                if isinstance(ob, dict) and ob.get('observation_id'):
                    by_seq[int(ob['observation_id'].split(':')[-1])] = Observation.model_validate(ob)

            pre_obs, arr_obs = by_seq[pre_seq], by_seq[arr_seq]
            hole_pre = next(o for o in pre_obs.objects if o.object_id == 'square-hole-1')
            cube_arr = next(o for o in arr_obs.objects if o.object_id == 'square-1')
            retained_fx = pre_obs.fixture.model_copy(update={'source': 'retained_static_fixture'})

            # Build real CalibratedScenePipeline cache state
            pipe = object.__new__(CalibratedScenePipeline)
            pipe.config = copy.deepcopy(scene_cfg)
            pipe.config.setdefault('calibration_id', pre_obs.calibration_id)
            pipe.config.setdefault('hole_id', 'square-hole-1')
            pipe.config.setdefault('native_frame', hole_pre.frame_id)
            pipe.coarse, pipe.development = True, False
            pipe.context_identity = ('o', 's', 't', 'h', 1, 1)
            pipe.acquisition_identity = pre_obs.acquisition_instance
            pipe.cached_aperture, pipe.last_timing = None, {}
            ctx = dict(
                authorized=True, placement_verified=False,
                grasp_contact=dict(request_id=close_id, published_ns=close_ns),
            )
            pipe._update_cached_aperture(
                pre_obs, base_hole=transform(hole_pre.pose),
                hole_evidence=hole_pre.evidence_ids, held=True, fixture=pre_obs.fixture, context=ctx,
            )

            f22 = list(last_full or spec['gate']['full22'])
            cur16 = np.array(arr_obs.state16, dtype=float)
            f22[:14] = cur16[:14].tolist()
            clock_now = [int(arr_obs.received_ns)]
            ps = PlanService(task, fixture_cfg, sock_path, clock=lambda: clock_now[0])
            vs = VerifyService(task, clock=lambda: clock_now[0], fixture=fixture_cfg)

            # In-hand rigid attachment: eef_object_pose from arrival measurement
            eef_obj = transform(cube_arr.eef_object_pose)
            base_hole = transform(hole_pre.pose)
            tip_ref = transform(fixture_cfg['eef_tip_reference'])
            obj_tcp = np.linalg.inv(eef_obj) @ tip_ref
            grasp_tilt_deg = round(math.degrees(math.acos(float(np.clip(abs(eef_obj[2, 2]), -1.0, 1.0)))), 2)

            trec = {
                'source_trace': str(trace_path),
                'pre_transport_obs_id': pre_obs.observation_id,
                'arrival_obs_id': arr_obs.observation_id,
                'cache_measured_at_ns': int(pre_obs.received_ns),
                'eef_object_grasp_tcp_in_obj_mm': [round(float(x * 1000), 2) for x in obj_tcp[:3, 3]],
                'eef_object_z_axis_tilt_deg': grasp_tilt_deg,
                'stages': {},
                'blockers_found': [],
            }

            def make_intent(skill, obs, req_suffix):
                return SkillIntent(
                    request_id=f'h47-{label}-{req_suffix}',
                    session_id='s', owner_instance_id='o',
                    task_id=task.task_id, task_hash=digest(task),
                    observation_id=obs.observation_id, epoch=1,
                    scene_version=obs.scene_version, calibration_id=obs.calibration_id,
                    skill=skill, parameters={}, budget_ms=25000,
                )

            seq_counter = [1000]
            def next_seq():
                seq_counter[0] += 1
                return seq_counter[0]

            # ---------------- STAGE 1: ALIGN ----------------
            # Match production CalibratedScenePipeline: when head is occluded and retained_static_fixture is used,
            # observation carries only right-wrist frames/pairings (revalidation.py:496).
            arr_wrist_frames = tuple(f for f in arr_obs.frames if f.camera == 'right')
            arr_wrist_pairings = tuple(p for p in arr_obs.pairings if p.camera == 'right')
            arr_occluded_base = arr_obs.model_copy(update=dict(
                frames=arr_wrist_frames,
                pairings=arr_wrist_pairings,
                geometry_camera='right',
            ))
            cached_hole_align = pipe._resolve_cached_aperture(arr_occluded_base, held=True, current_fixture=retained_fx, context=ctx)
            align_cache_age = pipe.last_timing['cached_aperture']['age_s']
            obs_align = arr_occluded_base.model_copy(update=dict(
                objects=(cube_arr, cached_hole_align),
                fixture=retained_fx,
                geometry_scope='coarse_static_task',
                quality_warnings=('aperture_from_pre_transport_cache',),
                scene_version=digest(dict(rev=1, objects=[cube_arr.model_dump(), cached_hole_align.model_dump()],
                                          fixture=retained_fx.model_dump())),
            ))
            intent_align = make_intent('align', obs_align, 'align')
            t0 = time.perf_counter()
            prop_align = ps.dispatch('plan_skill', dict(
                intent=intent_align.model_dump(), observation=obs_align.model_dump(),
                full22=f22, held16=cur16.tolist(),
            ))
            align_plan_s = round(time.perf_counter() - t0, 3)
            # Revalidate
            obs_align_rv = make_fresh_obs(obs_align, seq=next_seq(), now_ns=clock_now[0] + 150_000_000,
                                          state16=cur16, objects=(cube_arr, cached_hole_align),
                                          fixture=retained_fx, quality_warnings=('aperture_from_pre_transport_cache',))
            clock_now[0] = obs_align_rv.received_ns + 50_000_000
            rv_align = ps.dispatch('revalidate_plan', dict(
                proposal=prop_align, original_observation=obs_align.model_dump(),
                observation=obs_align_rv.model_dump(), intent=intent_align.model_dump(),
                full22=f22, held16=cur16.tolist(), after_ns=obs_align.received_ns + 50_000_000,
            ))
            # Stream through real F1Owner/Ruckig
            align_after_ns = clock_now[0]
            cur16, end_ns, align_rows, align_audit = stream_approved_output(
                prop_align['targets'], f22, cur16, clock_now[0], spec, dispatcher,
                skill='align', request_id=intent_align.request_id, obs_dump=obs_align.model_dump(),
            )
            clock_now[0] = end_ns + 100_000_000
            f22[:14] = cur16[:14].tolist()
            # Post-align observation (block moved rigidly with EEF; head still occluded -> cached aperture)
            eef_post_align = transform(dispatcher.dispatch('forward_pose', dict(full22=f22, held16=cur16.tolist())))
            base_obj_post_align = eef_post_align @ eef_obj
            hole_obj_align = np.linalg.inv(base_hole) @ base_obj_post_align
            obs_probe_align = make_fresh_obs(obs_align, seq=next_seq(), now_ns=clock_now[0], state16=cur16,
                                             objects=(), fixture=retained_fx)
            cached_post_align = pipe._resolve_cached_aperture(obs_probe_align, held=True, current_fixture=retained_fx, context=ctx)
            cube_post_align = cube_arr.model_copy(update=dict(
                pose=tuple(base_obj_post_align.ravel()), held=True, inserted=False, inserted_hole_id=None, stationary=True,
            ))
            obs_post_align = make_fresh_obs(obs_align, seq=next_seq(), now_ns=clock_now[0], state16=cur16,
                                            objects=(cube_post_align, cached_post_align), fixture=retained_fx,
                                            quality_warnings=('aperture_from_pre_transport_cache',))
            vs.dispatch('update_scene', dict(observation=obs_post_align.model_dump()))
            res_align = vs.dispatch('verify_skill', dict(intent=intent_align.model_dump(), after_ns=align_after_ns))
            t_align = np.asarray(prop_align['targets'], float)
            align_ranking = prop_align['geometry_trace'][0].get('candidate_ranking') if prop_align.get('geometry_trace') else None
            trec['stages']['align'] = dict(
                planned=True, variant=prop_align.get('candidate_variant'),
                waypoints=int(len(t_align)),
                arm_r_j6_deg=[round(float(t_align[:, 12].min()), 2), round(float(t_align[:, 12].max()), 2)],
                plan_6000d_s=align_plan_s,
                cache_age_s=align_cache_age,
                revalidate_ok=True, revalidate_segments=rv_align['geometry_checks']['swept_segments'],
                output_audit=align_audit,
                verify_outcome=res_align['outcome'], verify_reason=res_align['reason'],
                post_align_hole_xy_mm=round(float(np.linalg.norm(hole_obj_align[:2, 3]) * 1000), 2),
                post_align_hole_z_mm=round(float(hole_obj_align[2, 3] * 1000), 2),
                candidate_ranking=align_ranking,
            )
            print('  ALIGN:', json.dumps({k: v for k, v in trec['stages']['align'].items() if k != 'candidate_ranking'}, ensure_ascii=False), flush=True)
            if align_ranking:
                for rr in align_ranking:
                    print('    align rank v=%s conv=%s margin=%s preview=%s' % (
                        rr.get('variant'), (rr.get('endpoint_ik') or {}).get('converged'),
                        (rr.get('endpoint_ik') or {}).get('min_margin_deg'),
                        (rr.get('insertion_preview') or {}).get('status'),
                    ), flush=True)

            # ---------------- STAGE 2: INSERT ----------------
            insert_cache_age = round((obs_post_align.received_ns - pre_obs.received_ns) / 1e9, 4)
            intent_insert = make_intent('insert', obs_post_align, 'insert')
            t0 = time.perf_counter()
            insert_planned = False
            insert_err = None
            insert_diag_obstacles = []
            try:
                prop_insert = ps.dispatch('plan_skill', dict(
                    intent=intent_insert.model_dump(), observation=obs_post_align.model_dump(),
                    full22=f22, held16=cur16.tolist(),
                ))
                insert_planned = True
                insert_plan_s = round(time.perf_counter() - t0, 3)
            except Exception as exc:
                insert_plan_s = round(time.perf_counter() - t0, 3)
                insert_err = str(exc)
                # Diagnose EXACT blocking obstacle for each of the 4 square symmetries of insert!
                from f1_harness.skills import _square_goals, check_motion_route
                ins_scene = ps._scene(obs_post_align, f22, cur16.tolist(), skill='insert')
                h_mat = transform(ins_scene.base_hole)
                obj_goal = h_mat.copy()
                obj_goal[:3, 3] += h_mat[:3, 2] * (ins_scene.object_insertion_reference_height_m - ins_scene.insertion_depth_m)
                for sym_idx, cand in enumerate(_square_goals(obj_goal, transform(ins_scene.base_eef), ins_scene.eef_object)):
                    try:
                        check_motion_route(ins_scene, [cand], eef_object=ins_scene.eef_object, skill='insert', variant=sym_idx)
                        insert_diag_obstacles.append(dict(sym=sym_idx, clear=True))
                    except Exception as diag_exc:
                        insert_diag_obstacles.append(dict(sym=sym_idx, clear=False, error=str(diag_exc)))

            if insert_planned:
                obs_ins_rv = make_fresh_obs(obs_post_align, seq=next_seq(), now_ns=clock_now[0] + 150_000_000,
                                            state16=cur16, objects=(cube_post_align, cached_post_align),
                                            fixture=retained_fx, quality_warnings=('aperture_from_pre_transport_cache',))
                clock_now[0] = obs_ins_rv.received_ns + 50_000_000
                rv_insert = ps.dispatch('revalidate_plan', dict(
                    proposal=prop_insert, original_observation=obs_post_align.model_dump(),
                    observation=obs_ins_rv.model_dump(), intent=intent_insert.model_dump(),
                    full22=f22, held16=cur16.tolist(), after_ns=obs_post_align.received_ns + 50_000_000,
                ))
                insert_after_ns = clock_now[0]
                cur16, end_ns, insert_rows, insert_audit = stream_approved_output(
                    prop_insert['targets'], f22, cur16, clock_now[0], spec, dispatcher,
                    skill='insert', request_id=intent_insert.request_id, obs_dump=obs_post_align.model_dump(),
                )
                clock_now[0] = end_ns + 100_000_000
                f22[:14] = cur16[:14].tolist()
                # Post-insert simulated block pose & exact production insertion check
                eef_post_ins = transform(dispatcher.dispatch('forward_pose', dict(full22=f22, held16=cur16.tolist())))
                base_obj_post_ins = eef_post_ins @ eef_obj
                hole_obj_ins = np.linalg.inv(base_hole) @ base_obj_post_ins
                c_yaw = math.atan2(hole_obj_ins[1, 0], hole_obj_ins[0, 0])
                c_yaw_err = abs((c_yaw + math.pi / 4) % (math.pi / 2) - math.pi / 4)
                c_tilt = math.acos(float(np.clip(hole_obj_ins[2, 2], -1, 1)))
                c_xy = float(np.linalg.norm(hole_obj_ins[:2, 3]))
                c_z = float(hole_obj_ins[2, 3])
                c_inserted = bool(
                    c_xy <= scene_cfg['xy_tolerance_m']
                    and insertion_depth_reached(c_z, scene_cfg['insertion_depth_m'],
                                                tolerance_m=scene_cfg.get('insertion_depth_tolerance_m', 0.0))
                    and c_yaw_err <= scene_cfg['yaw_tolerance_rad']
                    and c_tilt <= scene_cfg['tilt_tolerance_rad']
                )
                obs_probe_ins = make_fresh_obs(obs_post_align, seq=next_seq(), now_ns=clock_now[0], state16=cur16,
                                               objects=(), fixture=retained_fx)
                cached_post_ins = pipe._resolve_cached_aperture(obs_probe_ins, held=True, current_fixture=retained_fx, context=ctx)
                post_ins_cache_age = pipe.last_timing['cached_aperture']['age_s'] if cached_post_ins else None
                cube_post_ins = cube_arr.model_copy(update=dict(
                    pose=tuple(base_obj_post_ins.ravel()), held=True, inserted=c_inserted,
                    inserted_hole_id='square-hole-1' if c_inserted else None, stationary=True,
                ))
                obs_post_ins = make_fresh_obs(obs_post_align, seq=next_seq(), now_ns=clock_now[0], state16=cur16,
                                              objects=(cube_post_ins, cached_post_ins), fixture=retained_fx,
                                              quality_warnings=('aperture_from_pre_transport_cache',))
                vs.dispatch('update_scene', dict(observation=obs_post_ins.model_dump()))
                res_insert = vs.dispatch('verify_skill', dict(intent=intent_insert.model_dump(), after_ns=insert_after_ns))
                t_ins = np.asarray(prop_insert['targets'], float)
                trec['stages']['insert'] = dict(
                    planned=True, variant=prop_insert.get('candidate_variant'),
                    waypoints=int(len(t_ins)),
                    arm_r_j6_deg=[round(float(t_ins[:, 12].min()), 2), round(float(t_ins[:, 12].max()), 2)],
                    plan_6000d_s=insert_plan_s,
                    cache_age_s=insert_cache_age,
                    post_insert_cache_age_s=post_ins_cache_age,
                    revalidate_ok=True, revalidate_segments=rv_insert['geometry_checks']['swept_segments'],
                    output_audit=insert_audit,
                    inserted=c_inserted,
                    hole_xy_mm=round(c_xy * 1000, 3),
                    hole_z_mm=round(c_z * 1000, 3),
                    yaw_err_deg=round(math.degrees(c_yaw_err), 3),
                    tilt_err_deg=round(math.degrees(c_tilt), 3),
                    verify_outcome=res_insert['outcome'], verify_reason=res_insert['reason'],
                )
            else:
                trec['stages']['insert'] = dict(
                    planned=False, plan_6000d_s=insert_plan_s, cache_age_s=insert_cache_age,
                    error=insert_err, symmetry_diagnostics=insert_diag_obstacles,
                )
                trec['blockers_found'].append('insert_plan_failed:' + insert_err)
                # Keep probing downstream stages from post-align pose so we see every downstream blocker too!
                cached_post_ins = cached_post_align
                cube_post_ins = cube_post_align.model_copy(update=dict(inserted=True, inserted_hole_id='square-hole-1'))
                obs_post_ins = obs_post_align.model_copy(update=dict(objects=(cube_post_ins, cached_post_ins)))
            print('  INSERT:', json.dumps(trec['stages']['insert'], ensure_ascii=False), flush=True)

            # ---------------- STAGE 3: RELEASE ----------------
            release_cache_age = round((obs_post_ins.received_ns - pre_obs.received_ns) / 1e9, 4)
            intent_release = make_intent('release', obs_post_ins, 'release')
            t0 = time.perf_counter()
            prop_release = ps.dispatch('plan_skill', dict(
                intent=intent_release.model_dump(), observation=obs_post_ins.model_dump(),
                full22=f22, held16=cur16.tolist(),
            ))
            release_plan_s = round(time.perf_counter() - t0, 3)
            obs_rel_rv = make_fresh_obs(obs_post_ins, seq=next_seq(), now_ns=clock_now[0] + 150_000_000,
                                        state16=cur16, objects=(cube_post_ins, cached_post_ins),
                                        fixture=retained_fx, quality_warnings=('aperture_from_pre_transport_cache',))
            clock_now[0] = obs_rel_rv.received_ns + 50_000_000
            rv_release = ps.dispatch('revalidate_plan', dict(
                proposal=prop_release, original_observation=obs_post_ins.model_dump(),
                observation=obs_rel_rv.model_dump(), intent=intent_release.model_dump(),
                full22=f22, held16=cur16.tolist(), after_ns=obs_post_ins.received_ns + 50_000_000,
            ))
            release_after_ns = clock_now[0]
            cur16, end_ns, release_rows, release_audit = stream_approved_output(
                prop_release['targets'], f22, cur16, clock_now[0], spec, dispatcher,
                skill='release', request_id=intent_release.request_id, obs_dump=obs_post_ins.model_dump(),
            )
            clock_now[0] = end_ns + 100_000_000
            f22[:14] = cur16[:14].tolist()

            # NOW TEST EXACT PRODUCTION POST-RELEASE BEHAVIOR WHEN HEAD IS STILL OCCLUDED & GRIPPER IS OPEN (held=False)!
            pipe_rel = copy.deepcopy(pipe)
            obs_probe_rel = make_fresh_obs(obs_post_ins, seq=next_seq(), now_ns=clock_now[0], state16=cur16,
                                           objects=(), fixture=None, geometry_scope='coarse_object_verification')
            cached_after_open = pipe_rel._resolve_cached_aperture(obs_probe_rel, held=False, current_fixture=None, context=ctx)
            cache_after_open_diag = dict(pipe_rel.last_timing.get('cached_aperture') or {})
            cube_post_rel = cube_post_ins.model_copy(update=dict(held=False, eef_object_pose=None, stationary=True))
            obs_post_rel_occluded = make_fresh_obs(
                obs_post_ins, seq=next_seq(), now_ns=clock_now[0], state16=cur16,
                objects=(cube_post_rel,), fixture=None, geometry_scope='coarse_object_verification',
                quality_warnings=('fixture_unavailable:partial_lid_landmarks_unavailable',),
            )
            rel_verify_err = None
            try:
                require_geometry_identity(
                    obs_post_rel_occluded, goal=task.goal, admission_profile=task.admission_profile,
                    skill='release', verification=True, hole_id=task.hole_id,
                )
            except Exception as exc:
                rel_verify_err = str(exc)

            trec['stages']['release'] = dict(
                planned=True, variant=prop_release.get('candidate_variant'),
                waypoints=int(len(prop_release['targets'])),
                plan_6000d_s=release_plan_s,
                cache_age_s=release_cache_age,
                revalidate_ok=True, revalidate_segments=rv_release['geometry_checks']['swept_segments'],
                output_audit=release_audit,
                post_open_cache_result=cached_after_open is not None,
                post_open_cache_diag=cache_after_open_diag,
                post_release_occluded_verify_gate_error=rel_verify_err,
            )
            if rel_verify_err:
                trec['blockers_found'].append('release_post_verify_blocked_when_occluded:' + rel_verify_err)
            print('  RELEASE:', json.dumps(trec['stages']['release'], ensure_ascii=False), flush=True)

            # ---------------- STAGE 4: WITHDRAW_RESET ----------------
            # (a) Test current production TaskEngine & return_observation contracts when called right after release (before verify_placed)
            eng_probe = TaskEngine(EvidenceStore(tmp_dir / f'eng-{label}'), mode=Mode.FAKE, clock=lambda: clock_now[0])
            sess = eng_probe.open_session()['session_id']
            eng_probe.prepare_task(sess, task.model_dump())
            eng_probe.local_start(physical_event=True, feedback_healthy=True)
            eng_probe.completed_skills.update(('approach_grasp', 'grasp', 'verify_grasp', 'inspect_in_hand',
                                               'transfer_above_hole', 'align', 'insert', 'release'))
            eng_probe.update_observation(obs_post_rel_occluded)
            wr_engine_err = None
            try:
                eng_probe._check_intent(make_intent('withdraw_reset', obs_post_rel_occluded, 'wr-check').model_copy(
                    update=dict(session_id=sess, owner_instance_id=eng_probe.instance, epoch=eng_probe.epoch)
                ))
            except Exception as exc:
                wr_engine_err = str(exc)
            wr_return_obs_err = None
            try:
                observe_return(pipe_rel, obs_post_rel_occluded, eng_probe.status()['perception_context'])
            except Exception as exc:
                wr_return_obs_err = str(exc)

            # (b) Geometric & F1Owner/Ruckig + sweep_outputs feasibility probe if withdraw_reset receives coarse_fixture_return with hole_pre!
            head_frames_only = tuple(f for f in pre_obs.frames if f.camera == 'head')
            head_rgb_sha = next(f.blob_sha256 for f in head_frames_only if f.stream == 'rgb')
            head_dep_sha = next(f.blob_sha256 for f in head_frames_only if f.stream == 'depth')
            hole_return = SceneObject(
                object_id='square-hole-1', shape='square', pose=hole_pre.pose,
                frame_id=hole_pre.frame_id, planning_margin_m=hole_pre.planning_margin_m,
                evidence_ids=(head_rgb_sha, head_dep_sha), visible=True, stationary=True,
            )
            obs_return = make_fresh_obs(
                pre_obs, seq=next_seq(), now_ns=clock_now[0], state16=cur16,
                objects=(hole_return,), fixture=None,
                geometry_scope='coarse_fixture_return', geometry_camera='head',
            )
            intent_wr = make_intent('withdraw_reset', obs_return, 'withdraw')
            t0 = time.perf_counter()
            wr_planned = False
            wr_plan_err = None
            wr_plan_s = None
            wr_variant = None
            wr_wpts = None
            wr_rv_ok = None
            wr_rv_segs = None
            wr_audit = None
            wr_sweep_ok = None
            wr_sweep_detail = None
            try:
                prop_wr = ps.dispatch('plan_skill', dict(
                    intent=intent_wr.model_dump(), observation=obs_return.model_dump(),
                    full22=f22, held16=cur16.tolist(),
                ))
                wr_plan_s = round(time.perf_counter() - t0, 3)
                wr_planned = True
                wr_variant = prop_wr.get('candidate_variant')
                wr_wpts = int(len(prop_wr['targets']))
                wr_rv_frames = tuple(
                    f.model_copy(update=dict(received_ns=clock_now[0] + 150_000_000, sequence=f.sequence + 1))
                    for f in obs_return.frames
                )
                wr_rv_pairings = tuple(
                    p.model_copy(update=dict(rgb_sequence=p.rgb_sequence + 1, depth_sequence=p.depth_sequence + 1))
                    for p in obs_return.pairings
                )
                obs_wr_rv = obs_return.model_copy(update=dict(
                    observation_id=f'h47-sim:{next_seq()}',
                    received_ns=clock_now[0] + 150_000_000,
                    state_received_ns=clock_now[0] + 150_000_000,
                    frames=wr_rv_frames,
                    pairings=wr_rv_pairings,
                ))
                clock_now[0] = obs_wr_rv.received_ns + 50_000_000
                rv_wr = ps.dispatch('revalidate_plan', dict(
                    proposal=prop_wr, original_observation=obs_return.model_dump(),
                    observation=obs_wr_rv.model_dump(), intent=intent_wr.model_dump(),
                    full22=f22, held16=cur16.tolist(), after_ns=obs_return.received_ns + 50_000_000,
                ))
                wr_rv_ok = True
                wr_rv_segs = rv_wr['geometry_checks']['swept_segments']
                wr_after_ns = clock_now[0]
                wr_start_f22 = list(f22)
                cur16, end_ns, wr_rows, wr_audit = stream_approved_output(
                    prop_wr['targets'], f22, cur16, clock_now[0], spec, dispatcher,
                    skill='withdraw_reset', request_id=intent_wr.request_id, obs_dump=obs_return.model_dump(),
                )
                clock_now[0] = end_ns + 100_000_000
                f22[:14] = cur16[:14].tolist()
                # Run REAL sweep_outputs audit on withdraw_reset F1Owner outputs!
                sweep_events = [dict(
                    kind='harness_planning_started',
                    intent=intent_wr.model_dump(),
                    observation=obs_return.model_dump(),
                    full_reference=wr_start_f22,
                    held_reference=obs_return.state16,
                )]
                sw = sweep_outputs(wr_rows, sweep_events, ps, dispatcher)
                wr_sweep_ok = bool(sw) and all(s['passed'] for s in sw)
                wr_sweep_detail = [dict(skill=s['skill'], passed=s['passed'], samples=s.get('samples')) for s in sw]
            except Exception as exc:
                wr_plan_s = round(time.perf_counter() - t0, 3)
                wr_plan_err = str(exc)

            trec['stages']['withdraw_reset'] = dict(
                current_engine_precondition_error=wr_engine_err,
                current_return_observation_error=wr_return_obs_err,
                geometry_probe_planned=wr_planned,
                geometry_probe_error=wr_plan_err,
                variant=wr_variant,
                waypoints=wr_wpts,
                plan_6000d_s=wr_plan_s,
                revalidate_ok=wr_rv_ok,
                revalidate_segments=wr_rv_segs,
                output_audit=wr_audit,
                sweep_outputs_ok=wr_sweep_ok,
                sweep_outputs_detail=wr_sweep_detail,
            )
            if wr_engine_err:
                trec['blockers_found'].append('withdraw_reset_engine_precondition:' + wr_engine_err)
            if wr_return_obs_err:
                trec['blockers_found'].append('withdraw_reset_return_observation:' + wr_return_obs_err)
            print('  WITHDRAW_RESET:', json.dumps(trec['stages']['withdraw_reset'], ensure_ascii=False), flush=True)

            # ---------------- STAGE 5: VERIFY_PLACED (post-withdrawal) ----------------
            # (a) Reject cached aperture
            obs_vp_cached = make_fresh_obs(
                pre_obs, seq=next_seq(), now_ns=clock_now[0], state16=cur16,
                objects=(cube_post_rel, cached_post_ins), fixture=pre_obs.fixture,
                geometry_scope='coarse_static_task',
            )
            vp_cached_res = verify_postcondition('verify_placed', task, obs_vp_cached, f'h47-{label}-vp-cached')
            # (b) Accept fresh head measurement after arm has withdrawn (simulated feedback!)
            hole_fresh = hole_pre.model_copy(update=dict(visible=True, source=None, measured_at_ns=None))
            obs_vp_fresh = make_fresh_obs(
                pre_obs, seq=next_seq(), now_ns=clock_now[0], state16=cur16,
                objects=(cube_post_rel, hole_fresh), fixture=pre_obs.fixture,
                geometry_scope='coarse_static_task',
            )
            vp_fresh_res = verify_postcondition('verify_placed', task, obs_vp_fresh, f'h47-{label}-vp-fresh')
            # (c) Block dropped inside bucket (invisible to head) -> object_not_observed
            obs_vp_dropped = make_fresh_obs(
                pre_obs, seq=next_seq(), now_ns=clock_now[0], state16=cur16,
                objects=(hole_fresh,), fixture=pre_obs.fixture,
                geometry_scope='coarse_static_task',
            )
            vp_dropped_res = verify_postcondition('verify_placed', task, obs_vp_dropped, f'h47-{label}-vp-dropped')

            trec['stages']['verify_placed'] = dict(
                cached_aperture_rejected=(vp_cached_res.outcome != 'success'),
                cached_aperture_result=dict(outcome=vp_cached_res.outcome, reason=vp_cached_res.reason),
                simulated_fresh_visible_block_result=dict(outcome=vp_fresh_res.outcome, reason=vp_fresh_res.reason),
                simulated_dropped_invisible_block_result=dict(outcome=vp_dropped_res.outcome, reason=vp_dropped_res.reason),
                total_cache_span_s=round((clock_now[0] - pre_obs.received_ns) / 1e9, 3),
            )
            print('  VERIFY_PLACED:', json.dumps(trec['stages']['verify_placed'], ensure_ascii=False), flush=True)
            report['trials'][label] = trec
    finally:
        server.close()

    out_path = W / 'Claude-advice/HARNESS/evidence/20261127-H47-placement-dry-run/h47-results.json'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print('SAVED:', out_path, flush=True)


if __name__ == '__main__':
    main()

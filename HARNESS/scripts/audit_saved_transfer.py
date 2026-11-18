"""Offline saved transfer diagnosis and actual F1Owner/Ruckig command audit.

No ROS, sockets, devices, model API, or robot connection. The plant follows
published commands ideally; this is not physical accuracy or task acceptance.
Source observations and production configuration remain unchanged.
"""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import argparse
import hashlib
import json
import sys
import time

import numpy as np

from f1_harness.contracts import Observation, SkillIntent, Task
from f1_harness.frames import FrameChain
from f1_harness.geometry import transform
from f1_harness.numerical_service import NumericalDispatcher
from f1_harness.owner_bridge import numerical_failure_detail
from f1_harness.skill_services import PlanService
from f1_harness.skills import _motion_clear, candidate_variants
from f1_harness.trajectory import sample


def load_saved(run, output, fixture_overrides=None, event_line=80):
    """Bind saved inputs and model identities; no robot admission is constructed."""
    root = Path(__file__).resolve().parents[1]
    run, output = Path(run).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    path = run / 'collected/owner/harness/trace.jsonl'
    event = json.loads(path.read_text().splitlines()[event_line - 1])
    assert event['kind'] == 'harness_planning_started'
    assert event['intent']['skill'] == 'transfer_above_hole'
    config = json.loads((run / 'site/plan.local.json').read_text())
    fixture = deepcopy(config['fixture'])
    fixture.update(fixture_overrides or {})
    task = Task.model_validate(config['task'])
    spec = json.loads((run / 'site/numerics.local.json').read_text())['spec']
    spec['urdf'] = str(root.parent / 'F1-4242/web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf')
    spec['gate']['config']['_infra_root'] = str(root.parent / 'F1-4242')
    spec['gate']['runtime'] = str(output / 'native-model')
    spec['gate']['full22'] = event['full_reference']
    frame = fixture['frame_chain']
    model_files = {spec['urdf']: frame['urdf_sha256'],
        str(root.parent / 'F1-4242/configs/model_mapping.json'): frame['mapping_sha256']}
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == sha
               for p, sha in model_files.items())
    numeric = NumericalDispatcher(spec)
    # Development admission binds remote filesystem paths. This diagnostic
    # reconstructs only the checked same-model geometry service, not admission,
    # authority, queueing, controller ownership or live revalidation.
    service = PlanService.__new__(PlanService)
    service.task, service.fixture = task, fixture
    service.clock = time.monotonic_ns
    service.posture_prior = fixture.get('success_posture_prior')
    service.frames = FrameChain(transform(frame['T_ik_world_native']),
        transform(frame['T_ik_eef_native_eef']), frame['native_frame'], frame['ik_world_frame'])
    service.numerics = SimpleNamespace(call=lambda method, **kw: numeric.dispatch(method, kw))
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in [
        Path(__file__), path, run / 'site/plan.local.json', run / 'site/numerics.local.json',
        *sorted((root / 'src/f1_harness').glob('*.py'))]}
    hashes.update(model_files)
    return dict(service=service, numeric=numeric, event=event, spec=spec,
                observation=Observation.model_validate(event['observation']),
                source_sha256=hashes, fixture_overrides=fixture_overrides or {})


def audit_output(ctx, proposal, scene, output_owner, full, measured, clock, *, max_ticks=4000):
    """Use real owner/smoother and dense scene checks with memory-only transport."""
    numeric = ctx['numeric']
    rows, failures = [], []
    start_index, segment_start, endpoint_since = 0, clock[0], None
    trajectory, targets = proposal['trajectory'], proposal['targets']
    assert trajectory is not None
    before = measured.copy()
    previous = transform(numeric.dispatch('forward_pose', dict(full22=full.tolist(), held16=before.tolist())))
    for _ in range(max_ticks):
        clock[0] += .02
        target, _, done, end = sample(trajectory, targets,
            start_index=start_index, elapsed_s=clock[0] - segment_start)
        output_owner.approve(target, clock[0], enabled=(False, True))
        command, _, receipt = output_owner.publish(clock[0])
        command = np.asarray(command)
        count = max(1, int(np.ceil(np.max(abs(command[7:14] - before[7:14])) / .1)))
        for fraction in np.linspace(0., 1., count + 1)[1:]:
            q = before + fraction * (command - before)
            numeric.numerics.mapping.check_action(q)
            numeric.numerics.gate.check_preventive(q, full)
            actual = transform(numeric.dispatch('forward_pose', dict(full22=full.tolist(), held16=q.tolist())))
            if not _motion_clear(scene, previous, actual, scene.eef_object):
                failures.append(dict(time_s=clock[0], fraction=float(fraction), reason='actual_output_scene_sweep_blocked'))
            previous = actual
        rows.append(dict(time_s=clock[0], reference16=target.tolist(), submitted16=command.tolist(),
            measured16=measured.tolist(), publication_complete=receipt['publication_complete'],
            sample_count=count, simulation=True))
        measured[:] = command
        full[7:14] = command[7:14]
        before = command.copy()
        if failures:
            break
        reached = done and np.max(abs(command[7:14] - np.asarray(targets[end])[7:14])) < .005
        endpoint_since = clock[0] if reached and endpoint_since is None else endpoint_since if reached else None
        if endpoint_since is not None and clock[0] - endpoint_since >= .3:
            if end == len(targets) - 1:
                break
            start_index, segment_start, endpoint_since = end + 1, clock[0], None
    else:
        failures.append(dict(reason='offline_output_not_converged', time_s=clock[0]))
    return dict(passed=not failures and all(r['publication_complete'] for r in rows),
        failures=failures, rows=rows, dense_samples=sum(r['sample_count'] for r in rows))


def run_sequence(ctx, output, skills=('transfer_above_hole', 'align', 'insert'), *, variants=None):
    from f1dagger.robot_io import F1Owner, make_output
    from f1dagger.config import pico_paths
    output = Path(output)
    event, service, numeric = (ctx[k] for k in ('event', 'service', 'numeric'))
    full = np.asarray(event['full_reference'], float)
    measured = np.asarray(event['held_reference'], float)
    observation = ctx['observation']
    clock = [10.]
    records, all_rows = [], []
    class MemoryTransport:
        rmw_gids = {key:'offline' for key in ('joint_command','left_gripper_command','right_gripper_command')}
        own_gids = {}
        def prepare(self, key): pass
        def assert_exclusive_cached(self, key): pass
        def publish(self, key, value): pass
        def release(self, key): pass
    source = SimpleNamespace(feedback=lambda: (full.copy(), measured.copy(), (clock[0],)*16),
                             state_valid_mask=observation.state_valid_mask)
    smoother = make_output(ctx['spec']['gate']['config'])
    owner = F1Owner(MemoryTransport(), source, numeric.numerics.mapping, smoother, numeric.numerics.gate, powered=True)
    owner.preventive_boundary_recovery = True
    owner.initialize(clock[0]); owner.prepare()
    profile = pico_paths(ctx['spec']['gate']['config']['_infra_root']) / 'config/control.json'
    for module_name in ('f1dagger.robot_io', 'f1dagger.arm_output', 'f1dagger.harness_compute',
                        'f1dagger.constrained_ik', 'f1dagger.teleop', 'pico_f1.arm_output',
                        'pico_f1.trajectory', 'pico_f1.config'):
        path = Path(sys.modules[module_name].__file__).resolve()
        ctx['source_sha256'][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    ctx['source_sha256'][str(profile)] = hashlib.sha256(profile.read_bytes()).hexdigest()
    velocity_limit = smoother.planner.max_velocity
    try:
        for skill in skills:
            scene = service._scene(observation, full.tolist(), measured.tolist(), skill=skill)
            attempts = []
            choices = (variants.get(skill, list(candidate_variants(skill))) if isinstance(variants, dict)
                       else variants if variants is not None else list(candidate_variants(skill)))
            proposal = None
            for variant in choices:
                intent = SkillIntent.model_validate(event['intent']).model_copy(update=dict(skill=skill))
                begin = time.perf_counter()
                try:
                    proposal = service._solve_candidate(intent, scene, measured.tolist(), full.tolist(), variant)
                    attempts.append(dict(variant=variant, outcome='planned', compute_s=time.perf_counter()-begin))
                    break
                except Exception as exc:
                    attempts.append(dict(variant=variant, outcome='rejected', compute_s=time.perf_counter()-begin,
                                         diagnostic=numerical_failure_detail(exc), raw_error=str(exc)))
            record = dict(skill=skill, attempts=attempts, start_s=clock[0])
            records.append(record)
            if proposal is None:
                record['outcome'] = 'planning_failed'
                break
            (output / (skill+'-proposal.json')).write_text(json.dumps(proposal, indent=2)+'\n')
            audit = audit_output(ctx, proposal, scene, owner, full, measured, clock)
            all_rows.extend(dict(skill=skill, **row) for row in audit.pop('rows'))
            record.update(output_audit=audit, end_s=clock[0], outcome='passed' if audit['passed'] else 'output_failed')
            if not audit['passed']: break
            eef = transform(numeric.dispatch('forward_pose', dict(full22=full.tolist(), held16=measured.tolist())))
            objects = tuple(item.model_copy(update=dict(pose=tuple((eef@scene.eef_object).ravel())))
                if item.object_id==service.task.object_id else item for item in observation.objects)
            observation = observation.model_copy(update=dict(objects=objects, state16=tuple(measured)))
    finally:
        owner.close()
    speeds = np.diff([r['submitted16'][:14] for r in all_rows], axis=0)/.02 if len(all_rows)>1 else np.zeros((1,14))
    report = dict(schema='f1-saved-transfer-owner-audit-v1', physical_motion=False, real_experience_eligible=False,
        source_sha256=ctx['source_sha256'], fixture_overrides=ctx['fixture_overrides'], stages=records,
        requested_skills=list(skills), passed=len(records)==len(skills) and all(r['outcome']=='passed' for r in records),
        output_samples=len(all_rows), max_speed_deg_s=float(np.max(abs(speeds))), velocity_limit_deg_s=velocity_limit,
        profile=str(profile), profile_sha256=hashlib.sha256(profile.read_bytes()).hexdigest(),
        model_input='saved current full22 and held16 command anchor, current attachment retained',
        limitations=['Ideal actuator following; no actual image, force, plant, ROS or robot output.',
            'Feedback initializes at saved command, then ideally follows memory-only publications.',
            'Offline same-model service construction does not validate live deployment admission.',
            'Sampled speed and dense commanded geometry are not physical accuracy certification.'])
    report['passed'] &= report['max_speed_deg_s'] <= velocity_limit+1e-6
    (output/'owner-output.json').write_text(json.dumps(all_rows)+'\n')
    (output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--override', type=Path)
    parser.add_argument('--skills', nargs='+', default=['transfer_above_hole','align','insert'])
    parser.add_argument('--variants', nargs='+', type=int)
    args=parser.parse_args()
    override=json.loads(args.override.read_text()) if args.override else None
    ctx=load_saved(args.run,args.output,override)
    result=run_sequence(ctx,args.output,args.skills,variants=args.variants)
    print(json.dumps({k:result[k] for k in ('passed','stages','output_samples','max_speed_deg_s')},indent=2))
    return 0 if result['passed'] else 1


if __name__=='__main__': raise SystemExit(main())

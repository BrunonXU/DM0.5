"""Offline rejection-sampling audit of frozen 50K failure observations.

No ROS, network client, robot command or training. Every candidate is an actual
model sample; no EEF target projection. Expanded IK is a separate research gate.
Frozen observations do not respond to alternative trajectories.
"""
import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np
from PIL import Image


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('inputs', 'output', 'bundle', 'core', 'compat'):
        p.add_argument('--'+name, type=Path, required=True)
    p.add_argument('--trials', type=int, default=16)
    p.add_argument('--seed-start', type=int, default=2026102100)
    p.add_argument('--request-ids', type=int, nargs='*')
    p.add_argument('--unguided', action='store_true')
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    # Use the frozen, previously verified H20 precision/graph compatibility code.
    exec(compile(args.compat.read_text().split('from f1_toys.server import main')[0],
                 str(args.compat), 'exec'), {'__file__': str(args.compat)})
    from f1_toys.inference import Policy
    from f1_toys.rtc_optimization import compile_suffix
    from f1_toys.eef import physical_actions, relative_pose
    from f1_toys.rtc import decode_window
    from f1_toys.native.kinematics import ContinuousIK, ArmModel, pose_error
    from f1_toys.native.fast_kinematics import FastArmModel
    from f1_toys.processing import sha256, process_image

    policy = Policy(args.bundle, args.core)
    compile_suffix(policy)
    config = Path(__import__('f1_toys.native.kinematics', fromlist=['__file__']).__file__).with_name('ccs_m6_40.MvKDCfg')
    ref_fk = ArmModel(config)
    expanded_model = FastArmModel(config)
    expanded_model.low[1] = np.deg2rad(-97.)
    expanded_ik = ContinuousIK(expanded_model)
    source = json.loads((args.inputs/'requests.json').read_text())
    images_index = {(r['episode'], r['request_id']): r for r in
                    map(json.loads, (args.inputs/'image-index.jsonl').read_text().splitlines())}
    if args.request_ids:
        source = [r for r in source if r['request']['request_id'] in args.request_ids]
    records = []
    source_hashes = {str(p): sha256(p) for p in
                     (Path(__file__), args.compat, config, args.bundle/'bundle.json',
                      args.inputs/'requests.json', args.inputs/'image-index.jsonl')}
    manifest = dict(model_bundle=policy.bundle_id, source_sha256=source_hashes,
                    seed_start=args.seed_start, trials=args.trials, guided=not args.unguided,
                    robot_commands=0, training_steps=0, production_changes=0,
                    limitations=['Original noise was not recorded; these are new samples.',
                        'Same recorded observations and committed prefix, not a closed-loop rollout.',
                        'Multiple samples need an independent scheduling/latency validation.',
                        'J2 -97 has no integrated-machine approval and is never sent to hardware.'])
    (args.output/'manifest.json').write_text(json.dumps(manifest, indent=2))
    stream = (args.output/'candidates.jsonl').open('x')
    first_call = True
    for selected in source:
        original = selected['request']
        entry = images_index[selected['episode'], original['request_id']]
        images = {}
        pixel_sha = {}
        for camera, name in entry['images'].items():
            path = args.inputs/name
            assert sha256(path) == entry['image_sha256'][camera]
            images[camera] = Image.open(path).convert('RGB')
            pixel_sha[camera] = hashlib.sha256(process_image(images[camera], camera, policy.contract).tobytes()).hexdigest()
        state = original['prediction_replay']['state16']
        held = original['prediction_replay']['last_submitted16']
        assert state == entry['observation']['state16']
        ctx = original['rtc_request']
        for trial in range(args.trials):
            began = time.perf_counter()
            result, raw, _ = policy.predict_rtc(images, state, held, ctx,
                                                seed=args.seed_start+trial,
                                                guided=not args.unguided)
            strict_elapsed = time.perf_counter()-began
            retry = None
            retry_ms = 0.
            if not result['success']:
                began = time.perf_counter()
                retry = decode_window(expanded_ik, physical_actions(raw, policy.stats['action']),
                                      state, held, ctx['committed_actions16'], ctx['coverage_steps'])
                retry_ms = (time.perf_counter()-began)*1000
            chosen = result if result['success'] else retry
            metrics = {}
            if chosen['success']:
                q = np.array(chosen['actions'])
                n = len(ctx['committed_actions16'])
                assert chosen['actions'][:n] == ctx['committed_actions16']
                previous = np.asarray(ctx['committed_actions16'][-1][7:14] if n else state[7:14])
                errors = []
                steps = []
                for k in range(n, len(q)):
                    err = pose_error(relative_pose(np.array(result['absolute_eef10'][k])), ref_fk.fk(1, q[k, 7:14]))
                    assert err[0] <= .01 and err[1] <= .001, err
                    errors.append(err)
                    steps.append(float(abs(q[k, 7:14]-previous).max()))
                    previous = q[k, 7:14]
                metrics = dict(max_joint_step_deg=max(steps), join_joint_step_deg=steps[0],
                               minimum_j2_deg=float(q[n:, 8].min()),
                               max_pose_error_mm_deg=np.max(errors, axis=0).tolist())
            record = dict(episode=selected['episode'], request_id=original['request_id'], trial=trial,
                          seed=args.seed_start+trial, bootstrap=ctx['bootstrap'],
                          guided=not args.unguided, strict_success=result['success'],
                          conditional_expanded_success=chosen['success'],
                          strict_failed_step=None if result['success'] else len(ctx['committed_actions16'])+len(result['diagnostics']),
                          measured_policy_ms=strict_elapsed*1000, expanded_retry_ms=retry_ms,
                          first_call_includes_warmup=first_call, pixels_sha256=pixel_sha,
                          timing_ms=result['timing_ms'], metrics=metrics,
                          absolute_eef10=result['absolute_eef10'],
                          raw32=raw.tolist(), actions=chosen['actions'],
                          selected_diagnostics=chosen['diagnostics'])
            first_call = False
            stream.write(json.dumps(record)+'\n'); stream.flush()
            records.append(record)
        subset = records[-args.trials:]
        print(json.dumps(dict(episode=selected['episode'], request_id=original['request_id'],
                              strict=sum(r['strict_success'] for r in subset),
                              expanded=sum(r['conditional_expanded_success'] for r in subset),
                              trials=args.trials, completed_requests=len(records)//args.trials)), flush=True)
    per_case=[]
    for selected in source:
        rows=[r for r in records if r['episode']==selected['episode'] and r['request_id']==selected['request']['request_id']]
        per_case.append(dict(episode=selected['episode'],request_id=selected['request']['request_id'],
                             trials=len(rows),strict_successes=sum(r['strict_success'] for r in rows),
                             expanded_successes=sum(r['conditional_expanded_success'] for r in rows)))
    summary=dict(cases=len(per_case), model_calls=len(records), per_case=per_case,
                 strict_cases_with_any=sum(r['strict_successes']>0 for r in per_case),
                 expanded_cases_with_any=sum(r['expanded_successes']>0 for r in per_case),
                 robot_commands=0, production_changes=0,
                 limitations=manifest['limitations'])
    (args.output/'summary.json').write_text(json.dumps(summary,indent=2))
    print(json.dumps({k:v for k,v in summary.items() if k!='per_case'}),flush=True)


if __name__ == '__main__':
    main()

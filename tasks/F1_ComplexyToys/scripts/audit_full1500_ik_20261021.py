"""Frozen Full1500 targets: boundary diagnosis and CPU-only solver controls."""
import hashlib
import json
import argparse
from pathlib import Path

import numpy as np
from f1_toys.eef import relative_pose
from f1_toys.native.kinematics import ArmModel, ContinuousIK, pose_error
from f1_toys.native.fast_kinematics import FastArmModel


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    task = Path(__file__).resolve().parents[1]
    root = task / 'Eval/artifacts/20261021-full1500-feedback'
    source = root / 'server-logs/requests.jsonl'
    journal = root / 'f1_hil_20261021T054754Z_e11beff0/journal'
    events = [json.loads(s) for s in (journal/'events.jsonl').open()]
    session = next(e['timing']['session_id'] for e in events if e['event']=='policy_transport_timing')
    rows = [r for r in map(json.loads, source.open()) if r['session_id']==session]
    failed = [r for r in rows if not r['success']]
    assert len(rows)==196 and len(failed)==16
    config = task/'src/f1_toys/native/ccs_m6_40.MvKDCfg'
    fk = ArmModel(config)
    models = {}
    for low in (-94, -95, -96, -97):
        model = FastArmModel(config); model.low[1] = np.deg2rad(low)
        models[low] = ContinuousIK(model)

    def decode(raw, low, first_reference=None):
        prefix = raw['rtc_request']['committed_actions16']
        ref = np.array(prefix[-1][7:14] if prefix else raw['prediction_replay']['state16'][7:14])
        diagnostics = []; qs = []; max_step = 0.
        for k in range(len(prefix), raw['rtc_request']['coverage_steps']):
            target = relative_pose(np.array(raw['absolute_eef10'][k]))
            seed = first_reference if k==len(prefix) and first_reference is not None else ref
            d = models[low].solve(1, target, seed); diagnostics.append(d)
            if not d['success']: break
            q = np.array(d['joints'])
            err = pose_error(target, fk.fk(1,q))
            assert err[0]<=.01 and err[1]<=.001 and models[low].model.admissible_rad(np.deg2rad(q))
            max_step = max(max_step, float(abs(q-ref).max())); ref = q; qs.append(q.tolist())
        return dict(full=len(prefix)+len(qs)==raw['rtc_request']['coverage_steps'],
                    valid_new=len(qs), max_true_reference_step_deg=max_step,
                    joints=qs, diagnostics=diagnostics)

    records = []
    for raw in failed:
        prefix = raw['rtc_request']['committed_actions16']
        ref = np.array(prefix[-1][7:14] if prefix else raw['prediction_replay']['state16'][7:14])
        controls = {str(low):decode(raw,low) for low in models}
        starts = []
        for axis in (0,2,4):
            for delta in (-3.,3.):
                guess=ref.copy(); guess[axis]+=delta
                starts.append(dict(axis=axis+1,delta_deg=delta,result=decode(raw,-94,guess)))
        d=raw['diagnostics'][-1]
        records.append(dict(request_id=raw['request_id'],policy_epoch=raw['policy_epoch'],
            bootstrap=raw['rtc_context']['bootstrap'], committed_points=len(prefix),
            original_valid_new=len(raw['diagnostics'])-1,
            original_failed_h50_index=len(prefix)+len(raw['diagnostics'])-1,
            original_failure=d, margins=raw['ik_limit_margins'][-1],
            observed_right=raw['prediction_replay']['state16'][7:14],
            submitted_right=raw['prediction_replay']['last_submitted16'][7:14],
            controls=controls,multistart=starts))
        print(json.dumps(dict(request_id=raw['request_id'],
            full={k:v['full'] for k,v in controls.items()},
            multistart_full=sum(s['result']['full'] for s in starts))),flush=True)
    summary=dict(session_id=session,model_id=rows[0]['model_id'],requests=len(rows),rejected=len(failed),
        rejected_ids=[r['request_id'] for r in failed],
        all_failed_candidates_touch_j2_lower=all(abs(r['original_failure']['candidate_joints'][1]+94)<1e-6 for r in records),
        all_failed_candidates_touch_wrist_coupling=all(abs(r['margins']['wrist_coupling_deg'])<1e-6 for r in records),
        observed_j6_range_deg=[min(r['observed_right'][5] for r in records),max(r['observed_right'][5] for r in records)],
        original_valid_new_nonzero=sum(r['original_valid_new']>0 for r in records),
        full_by_j2_lower={str(low):sum(r['controls'][str(low)]['full'] for r in records) for low in models},
        full_with_any_of_six_strict_multistarts=sum(any(s['result']['full'] for s in r['multistart']) for r in records),
        robot_commands=0,model_calls=0,online_changes=0,
        limitations=['Frozen targets and prefix; not a counterfactual closed loop or task success.',
          'A local solver failure is not proof of globally unreachable pose.',
          'Expanded limits are diagnostic only; F1-09 driver/hardware authorization unavailable.',
          'Multistart preserves EEF but may change joint path; no motion/collision/timing certification.'])
    out=args.output;out.mkdir(parents=True,exist_ok=False)
    (out/'results.json').write_text(json.dumps(records,indent=2)+'\n')
    (out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    sources=[source,journal/'events.jsonl',config,Path(__file__)]
    (out/'manifest.json').write_text(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},indent=2)+'\n')
    print(json.dumps(summary,indent=2),flush=True)


if __name__=='__main__': main()

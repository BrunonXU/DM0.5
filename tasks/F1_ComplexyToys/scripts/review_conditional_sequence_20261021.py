"""CPU-only frozen-target sequence audit; does not generate robot feedback.

Carry computed plans across every verifiable historical guided dependency.
Historical bootstrap is an explicit reset, never a continuation claim. For a
rescued rejection extend its own q path through remaining H50 targets, without
new prediction or changing the immutable committed prefix.
"""
import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np
from f1_toys.eef import relative_pose
from f1_toys.native.kinematics import ArmModel, ContinuousIK, pose_error
from f1_toys.native.fast_kinematics import FastArmModel


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    task = Path(__file__).resolve().parents[1]
    source = task/'Eval/artifacts/20261020-50k-ik/joined-requests.jsonl'
    config = task/'src/f1_toys/native/ccs_m6_40.MvKDCfg'
    rows = [json.loads(line) for line in source.read_text().splitlines()]
    fk = ArmModel(config)
    strict = ContinuousIK(FastArmModel(config))
    extended_model = FastArmModel(config)
    extended_model.low[1] = np.deg2rad(-97.)
    expanded = ContinuousIK(extended_model)
    originals = {(x['episode'], x['server']['request_id']): x['server'] for x in rows}
    plans, records, links = {}, [], []

    def decode(raw, prefix, ik, start=None, stop=None):
        start = len(prefix) if start is None else start
        stop = raw['rtc_request']['coverage_steps'] if stop is None else stop
        qs = [list(q) for q in prefix]
        ref = np.array(qs[-1] if qs else raw['prediction_replay']['state16'][7:14])
        diagnostics = []
        t0 = time.perf_counter()
        for k in range(start, stop):
            d = ik.solve(1, relative_pose(np.array(raw['absolute_eef10'][k])), ref)
            d['step_1_based'] = k+1
            diagnostics.append(d)
            if not d['success']:
                break
            ref = np.array(d['joints'])
            qs.append(ref.tolist())
        return dict(success=len(qs)==stop, qs=qs, diagnostics=diagnostics,
                    elapsed_ms=(time.perf_counter()-t0)*1000)

    def verify(raw, result, start, ik):
        errors = []
        for k in range(start, len(result['qs'])):
            q = np.array(result['qs'][k])
            err = pose_error(relative_pose(np.array(raw['absolute_eef10'][k])), fk.fk(1,q))
            assert err[0] <= .01 and err[1] <= .001
            assert ik.model.admissible_rad(np.deg2rad(q))
            errors.append(err)
        return np.max(errors, axis=0).tolist() if errors else [0.,0.]

    for row in rows:
        raw, episode = row['server'], row['episode']
        rtc, rid = raw['rtc_request'], raw['request_id']
        key = (episode,rid)
        recorded_prefix = np.array(rtc['committed_actions16'])
        if rtc['bootstrap']:
            assert len(recorded_prefix) == 0 and rtc['base_plan_id']==0
            prefix = []
        else:
            basekey = (episode,rtc['base_plan_id'])
            base = originals[basekey]
            assert base['success'] and base['policy_epoch']==raw['policy_epoch']
            shift, delay = rtc['shift_steps'], rtc['delay_steps']
            expected = np.array(base['actions'])[shift:shift+delay,7:14]
            np.testing.assert_allclose(expected, recorded_prefix[:,7:14], rtol=0,atol=1e-10)
            prefix = plans[basekey]['qs'][shift:shift+delay]
            assert len(prefix)==delay
            origin_error = None
            if not base['rtc_request']['bootstrap']:
                origin_error = (rtc['plan_origin_ns']-base['rtc_request']['plan_origin_ns'])-shift/30*1e9
                assert abs(origin_error)<2
            links.append(dict(episode=episode,request_id=rid,base_plan_id=rtc['base_plan_id'],
                shift=shift,delay=delay,origin_grid_error_ns=origin_error,
                candidate_prefix_difference_deg=float(np.max(abs(np.array(prefix)-recorded_prefix[:,7:14])))))
        first = decode(raw,prefix,strict)
        second = None if first['success'] else decode(raw,prefix,expanded)
        selected = first if first['success'] else second
        assert selected['qs'][:len(prefix)] == prefix
        errors = verify(raw,selected,len(prefix),strict if first['success'] else expanded)
        rec = dict(episode=episode,request_id=rid,epoch=raw['policy_epoch'],bootstrap=rtc['bootstrap'],
            base_plan_id=rtc['base_plan_id'],original_success=raw['success'],success=selected['success'],
            expanded_called=second is not None,strict_ms=first['elapsed_ms'],
            total_ms=first['elapsed_ms']+(second['elapsed_ms'] if second else 0),
            prefix=prefix,selected=selected,verified_max_pose_errors=errors)
        if selected['success']:
            plans[key] = selected
        if raw['success']:
            rec['difference_recorded_q_deg'] = float(np.max(abs(np.array(selected['qs'])-np.array(raw['actions'])[:,7:14]))) if selected['success'] else None
        elif selected['success']:
            # Same predicted absolute targets, genuinely seed every tail point
            # from the newly computed candidate, not historical measured q.
            tail = decode(raw,selected['qs'],expanded,stop=len(raw['absolute_eef10']))
            tail['verified_max_pose_errors'] = verify(raw,tail,len(selected['qs']),expanded)
            rec['candidate_own_H50_tail'] = tail
        records.append(rec)
    failed = [r for r in records if not r['original_success']]
    rescued = [r for r in failed if r['success']]
    controls = [r for r in records if r['original_success']]
    stats = lambda v: dict(p50=float(np.percentile(v,50)),p95=float(np.percentile(v,95)),max=max(v))
    diag = [d for r in records for d in r['selected']['diagnostics'] if d['success']]
    taildiag = [d for r in rescued for d in r['candidate_own_H50_tail']['diagnostics'] if d['success']]
    summary = dict(requests=len(records),verified_guided_links=len(links),
        historical_bootstrap_resets=sum(r['bootstrap'] for r in records),
        original_success_count=len(controls),original_success_retained=sum(r['success'] for r in controls),
        max_control_q_change_deg=max(r['difference_recorded_q_deg'] or 0 for r in controls),
        historical_rejections=len(failed),rescued_K42=len(rescued),
        rescued_H50=sum(r['candidate_own_H50_tail']['success'] for r in rescued),
        remaining_K42_failures=[dict(episode=r['episode'],request_id=r['request_id'],failed_step=r['selected']['diagnostics'][-1]['step_1_based']) for r in failed if not r['success']],
        newly_failing_H50_tail=[dict(episode=r['episode'],request_id=r['request_id'],failed_step=r['candidate_own_H50_tail']['diagnostics'][-1]['step_1_based']) for r in rescued if not r['candidate_own_H50_tail']['success']],
        max_candidate_step_deg=max(d['reference_step_max_deg'] for d in diag),
        max_tail_step_deg=max(d['reference_step_max_deg'] for d in taildiag),
        max_guided_prefix_difference_deg=max(l['candidate_prefix_difference_deg'] for l in links),
        rejected_K42_timing_ms=stats([r['total_ms'] for r in failed]),
        H50_tail_timing_ms=stats([r['candidate_own_H50_tail']['elapsed_ms'] for r in rescued]),
        limitations=[
            'All guided dependencies begin at an original successful base plan. Conditional expansion first changes a historical rejected terminal window; subsequent historical requests are bootstrap, not valid candidate continuations.',
            'Reconstructed existing guided prefixes are carried exactly, but bootstrap resets deliberately use historical measured state. No sequence is claimed continuous across those resets.',
            'H50 extension carries candidate joints through the same fixed prediction only; no new model prediction, changed observations or feedback.',
            'K42 failures do not imply all H50 targets infeasible; local solver only. K42/H50 success is not task success.',
            'No collision, tracking, velocity/acceleration acceptance, approved hardware expansion, or online total timing certification.'],
        model_calls=0,robot_commands=0,gpu_calls=0,deployment_changes=0)
    (args.output/'results.json').write_text(json.dumps(dict(summary=summary,links=links,records=records),indent=2)+'\n')
    (args.output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    sources = [Path(__file__),source,config,task/'src/f1_toys/native/kinematics.py',task/'src/f1_toys/native/fast_kinematics.py']
    (args.output/'manifest.json').write_text(json.dumps(dict(sources={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},model_calls=0,robot_commands=0,gpu_calls=0),indent=2)+'\n')
    print(json.dumps(summary),flush=True)


if __name__=='__main__':
    main()

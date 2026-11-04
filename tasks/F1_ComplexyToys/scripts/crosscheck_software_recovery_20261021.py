#!/usr/bin/env python3
"""Read-only original strict partial versus frozen16-seed strict full coverage."""
import collections
import hashlib
import json
from pathlib import Path
TASK=Path(__file__).resolve().parents[1]
BASE=TASK/'Eval/artifacts/20261021-ik-recovery-next'

def main():
    progress_path=BASE/'partial/progress-hold-results.json'
    sample_path=BASE/'resample-guided16/candidates.jsonl'
    raw_path=TASK/'Eval/artifacts/20261020-50k-ik/joined-requests.jsonl'
    progress=json.loads(progress_path.read_text())
    samples=collections.defaultdict(list)
    for line in sample_path.read_text().splitlines():
        r=json.loads(line);samples[(r['episode'],r['request_id'])].append(r)
    original={(r['episode'],r['server']['request_id']):r['server'] for r in map(json.loads,raw_path.read_text().splitlines())}
    cross=[];gripper=[]
    for r in progress['records']:
        k=(r['episode'],r['request_id']);s=sorted(samples[k],key=lambda x:x['trial'])
        assert len(s)==16 and len({x['trial'] for x in s})==16
        p=next(c for c in r['cases'] if c['j2_lower_candidate'] is None)
        raw=original[k];ctx=raw['rtc_request'];n_prefix=len(ctx['committed_actions16'])
        stricts=[x for x in s if x['strict_success']]
        lengths=[dict(trial=x['trial'],seed=x['seed'],valid_new_points=(ctx['coverage_steps']-n_prefix if x['strict_success']
            else x['strict_failed_step']-1-n_prefix),strict_full=x['strict_success']) for x in s]
        assert all(x['valid_new_points']>=0 for x in lengths)
        best=max(lengths,key=lambda x:x['valid_new_points'])
        cross.append(dict(episode=k[0],request_id=k[1],original_valid_new_points=p['valid_new_points'],
            strict_full_successes_of16=len(stricts),strict_full_first_trial=stricts[0]['trial'] if stricts else None,
            union_original_partial_or_resampled_strict_full=p['valid_new_points']>0 or bool(stricts),
            max_resampled_strict_valid_new_points=best['valid_new_points'],max_prefix_trial=best['trial'],
            max_prefix_seed=best['seed'],all_resampled_prefix_lengths=lengths,
            prefix_evidence='counts_from_strict_failed_step; selected_diagnostics_may_be_expanded_and_must_not_be_reused_as_strict_q'))
        if p['valid_new_points']==0:continue
        start=ctx['committed_actions16'][-1][15] if n_prefix else raw['prediction_replay']['last_submitted16'][15]
        sequence=[max(0.,min(100.,x[9]*50+50)) for x in raw['absolute_eef10'][n_prefix:n_prefix+p['valid_new_points']]]
        assert len(sequence)==p['valid_new_points']
        # Keep every generated value under existing decoder scaling/clipping;
        # no new freeze/clamp, skipping of transition points or reversed replay.
        crossing=p['planned_gripper_crosses_half']
        lower=min([start]+sequence);upper=max([start]+sequence)
        gripper.append(dict(episode=k[0],request_id=k[1],valid_new_points=p['valid_new_points'],
            reference_gripper=start,all_original_valid_gripper_targets=sequence,
            policy='retain_entire_original_valid_gripper_sequence_with_same_path_phase; no_recovery_specific_value_change',
            grouping='crosses_half_task_phase_required' if crossing else 'no_half_crossing_not_grasp_or_contact_certified',
            first_half_crossing_new_step=p['first_gripper_half_crossing_new_step'],
            min=lower,max=upper,range=upper-lower,
            descriptive_band='low_0_to10' if upper<=10 else ('high_90_to100' if lower>=90 else 'other'),
            exact_constant_sequence=upper==lower,
            partial_stop_rule='Select a certified stopping endpoint only with task-phase evidence; stopping before a transition yields only a partial task, never claim all gripper intent executed.',
            transition_rule='If retained path crosses open/close, do not freeze or silently omit it to call transport safe; phase/contact/object evidence required before motion.' if crossing else
                'Small numeric variation or constant endpoint command does not establish grasp, object pose or no contact. Retain values and require dynamic stop certification.'))
    zero=[x for x in cross if x['original_valid_new_points']==0]
    uncovered=[x for x in cross if not x['union_original_partial_or_resampled_strict_full']]
    partial_after_full_selection=[x for x in cross if x['strict_full_successes_of16']==0 and x['original_valid_new_points']>0]
    grip_by_key={(x['episode'],x['request_id']):x for x in gripper}
    result=dict(schema='software_recovery_strict_crosscheck_v1',date='2026-10-21',
        summary=dict(requests=49,original_strict_partial_nonzero=38,original_strict_partial_zero=11,
            zero_original_with_strict_resample_full=sum(x['strict_full_successes_of16']>0 for x in zero),
            original_partial_or_resampled_strict_full=sum(x['union_original_partial_or_resampled_strict_full'] for x in cross),
            neither_original_partial_nor_resampled_strict_full=len(uncovered),
            neither_group_with_recorded_resampled_strict_nonzero_prefix=sum(x['max_resampled_strict_valid_new_points']>0 for x in uncovered),
            strict_full_candidate_cases=sum(x['strict_full_successes_of16']>0 for x in cross),
            original_partial_cases_after_strict_full_selection=len(partial_after_full_selection),
            original_partial_after_full_selection_half_crossings=sum(grip_by_key[(x['episode'],x['request_id'])]['grouping']=='crosses_half_task_phase_required' for x in partial_after_full_selection),
            gripper_half_crossing=sum(x['grouping']=='crosses_half_task_phase_required' for x in gripper),
            gripper_no_half_crossing=sum(x['grouping']!='crosses_half_task_phase_required' for x in gripper),
            gripper_descriptive_bands=dict(collections.Counter(x['descriptive_band'] for x in gripper)),
            gripper_exact_constant=sum(x['exact_constant_sequence'] for x in gripper),
            all_gripper_sequences_preserved_without_recovery_value_edits=True,
            actual_stopping_certified=0,actual_recovery_successes=0),
        original_zero_cases=zero,remaining_without_original_partial_or_full_resample=uncovered,
        original_partial_after_strict_full_selection=partial_after_full_selection,
        cases=cross,gripper_groups=gripper,
        conclusions=[
            'Software-only union has evidence of an original strict valid segment or a strict fully solvable seed in46/49 frozen cases. This is mixed evidence of opportunity, not46 recovery successes.',
            'Remaining64184400 requests75/77/78 lack original valid suffix and full strict sampled candidates, but strict_failed_step reports new candidate partial lengths19/5/20. Re-decode their selected raw candidate under original strict bounds to obtain and independently verify actual q; do not borrow expanded selected_diagnostics.',
            'Original38 valid segments retain original gripper targets exactly. Two segments cross50:3dd2babb38 at first newpoint and63 at23rd. They require explicit task-phase/contact evidence before executing the transition; no silent gripper freezing.',
            'Other36 segments show no half crossing, but this does not establish safe transport, maintained grasp or lack of insertion contact. All require stopping-path/actual-state verification; grouping is diagnostic.',
            'Original gripper grouping does not describe newly sampled model candidates. Each selected new candidate needs its own gripper/phase audit.',
            'No driver expansion is needed for the strict crosscheck. Hardware expansion evidence is excluded from headline counts.',
            'Same-observation resampling is different from progress-HOLD-freshbootstrap. No future observations or task outcomes are known here; the next policy can remain wrong.'
        ],robot_commands=0,model_calls=0,production_changes=0)
    out=BASE/'partial/software-crosscheck.json';out.write_text(json.dumps(result,indent=2)+'\n')
    (BASE/'partial/software-crosscheck-manifest.json').write_text(json.dumps(dict(sources=[dict(path=str(p),sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in [progress_path,sample_path,raw_path,Path(__file__)]]),indent=2)+'\n')
    print(json.dumps(result['summary'],indent=2))
if __name__=='__main__':main()

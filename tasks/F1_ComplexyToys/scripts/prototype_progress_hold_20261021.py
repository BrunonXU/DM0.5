#!/usr/bin/env python3
"""Offline finite valid-prefix progress then confirmed HOLD/reobserve prototype.

No robot or model APIs. StopCertificate values in unit scenarios are synthetic
validation contracts, never generated/accepted hardware stop certificates.
"""
import ast
from dataclasses import dataclass,replace,asdict
import hashlib
import json
import math
from pathlib import Path
import numpy as np
TASK=Path(__file__).resolve().parents[1]
OUT=TASK/'Eval/artifacts/20261021-ik-recovery-next/partial'


def reference_model():
    # Reuse the exact pinned ArmModel AST, excluding unrelated scipy IK imports.
    # This avoids installing an environment or writing/reimplementing FK math.
    source=TASK/'src/f1_toys/native/kinematics.py'
    nodes=[]
    for node in ast.parse(source.read_text()).body:
        if isinstance(node,ast.ClassDef) and node.name=='ArmModel':nodes.append(node)
        elif isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id in
                {'LOW_DEG','HIGH_DEG','CONFIG_SHA256'} for t in node.targets):nodes.append(node)
    namespace=dict(np=np,hashlib=hashlib,Path=Path)
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(source),'exec'),namespace)
    return namespace['ArmModel'](TASK/'src/f1_toys/native/ccs_m6_40.MvKDCfg')


def rotation_deg(a,b):
    return math.degrees(math.acos(float(np.clip((np.trace(a.T@b)-1)/2,-1,1))))


def targets(raw,diags,model):
    prefix=raw['rtc_request']['committed_actions16']
    start=np.array(prefix[-1][7:14] if prefix else raw['prediction_replay']['state16'][7:14])
    grip0=prefix[-1][15] if prefix else raw['prediction_replay']['last_submitted16'][15]
    qs=[start];poses=[model.fk(1,start)];grips=[grip0];target_positions=[]
    for i,d in enumerate(diags):
        if not d['success']:
            assert i==len(diags)-1
            break
        q=np.array(d['joints']);pose=model.fk(1,q)
        target=raw['absolute_eef10'][len(prefix)+i]
        poserr=float(np.linalg.norm(pose[:3,3]-target[:3])*1000)
        # Rotation6D target matrix uses orthonormalized rows as native EEF codec.
        first=np.array(target[3:6]);first/=np.linalg.norm(first)
        second=np.array(target[6:9]);second-=np.dot(first,second)*first;second/=np.linalg.norm(second)
        rot=np.stack([first,second,np.cross(first,second)])
        roterr=rotation_deg(rot,pose[:3,:3])
        assert poserr<=.01 and roterr<=.001,(poserr,roterr)
        qs.append(q);poses.append(pose);grips.append(min(100.,max(0.,target[9]*50+50)))
        target_positions.append(target[:3])
    delta=[float(np.linalg.norm(b[:3,3]-a[:3,3])*1000) for a,b in zip(poses,poses[1:])]
    angular=[rotation_deg(a[:3,:3],b[:3,:3]) for a,b in zip(poses,poses[1:])]
    crossings=[i for i in range(1,len(grips)) if (grips[i-1]<50<=grips[i]) or (grips[i]<50<=grips[i-1])]
    endpoints=[dict(new_step=i,translation_from_reference_mm=float(np.linalg.norm(poses[i][:3,3]-poses[0][:3,3])*1000),
                    rotation_from_reference_deg=rotation_deg(poses[0][:3,:3],poses[i][:3,:3]),
                    gripper=grips[i],joint_target=qs[i].tolist()) for i in range(1,len(qs))]
    return dict(valid_new_points=len(qs)-1,valid_total=len(prefix)+len(qs)-1,
        reference='last_immutable_joint_target' if prefix else 'measured_request_state',
        endpoint_translation_mm=float(np.linalg.norm(poses[-1][:3,3]-poses[0][:3,3])*1000),
        polyline_translation_mm=sum(delta),endpoint_rotation_deg=rotation_deg(poses[0][:3,:3],poses[-1][:3,:3]),
        polyline_rotation_deg=sum(angular),
        endpoint_joint_max_deg=float(np.max(abs(qs[-1]-qs[0]))),
        max_adjacent_joint_step_deg=max((float(np.max(abs(b-a))) for a,b in zip(qs,qs[1:])),default=0),
        max_excursion_translation_mm=max(float(np.linalg.norm(p[:3,3]-poses[0][:3,3])*1000) for p in poses),
        endpoint_gripper_delta=grips[-1]-grips[0],gripper_min=min(grips),gripper_max=max(grips),
        planned_gripper_crosses_half=min(grips)<50<=max(grips),
        first_gripper_half_crossing_new_step=crossings[0] if crossings else None,
        before_first_gripper_half_crossing=(endpoints[crossings[0]-2] if crossings and crossings[0]>1 else None),
        candidate_endpoint_geometry_only=endpoints,
        original_immutable_prefix_count=len(prefix),
        new_joint_targets=[q.tolist() for q in qs[1:]],
        original_eef_positions_m=target_positions,
        nominal_new_segment_ms=(len(qs)-1)/30*1000)


@dataclass(frozen=True)
class Recovery:
    state:str='running'
    owner:str='policy_owner'
    epoch:int=4
    cycles:int=0
    no_progress_cycles:int=0
    max_cycles:int=3
    valid_new:int=0
    hold_confirmed_ns:int=0
    reject_request:int=0


def transition(s,event):
    """Contract-only state reducer. It returns state/reason, never motion commands.

    Existing owner retains authority; policy sampling is suspended during progress.
    Stop certification and measured settling are caller responsibilities that are
    explicitly required here and are *not* implemented by the reducer.
    """
    kind=event['kind'];now=event.get('now_ns',0)
    if event.get('owner',s.owner)!=s.owner:
        return replace(s,state='handoff_abort'),'owner_changed_do_not_publish'
    if event.get('epoch',s.epoch)!=s.epoch:
        return s,'stale_epoch_discarded'
    if kind=='fault':
        return replace(s,state='hold_required'),'existing_output_fault_hold'
    if s.state=='running' and kind=='verified_ik_rejection':
        if event['valid_new']<=0:
            return replace(s,state='hold_required'),'no_new_valid_point'
        if s.cycles>=s.max_cycles or s.no_progress_cycles>=2 or event.get('same_stalled_state',False):
            return replace(s,state='paused'),'bounded_recovery_or_stalled_state'
        if not event.get('prefix_exact') or not event.get('before_original_adoption_deadline'):
            return replace(s,state='hold_required'),'invalid_partial_adoption'
        return replace(s,state='await_stop_certificate',valid_new=event['valid_new'],reject_request=event['request_id']),'freeze_new_model_requests_keep_current_owner'
    if s.state=='await_stop_certificate' and kind=='stop_certificate':
        required=['actual_qva_fresh','matches_state_and_plan','immutable_prefix_times_exact',
                  'stays_within_valid_path','limits_and_wrist_valid','continuous_qva_and_bounded_jerk',
                  'gripper_phase_semantics_valid','stop_before_failed_target','fallback_stop_from_every_sample']
        if not all(event.get(k,False) for k in required):
            return replace(s,state='hold_required'),'missing_dynamic_stop_certificate'
        return replace(s,state='progress_then_stop',cycles=s.cycles+1),'allow_only_certified_finite_segment_no_future_inference'
    if s.state=='progress_then_stop' and kind=='stop_confirmed':
        if not all(event.get(k,False) for k in ['measured_settled','measured_hold_committed','feedback_after_final_command']):
            return s,'wait_for_actual_settling_and_hold'
        count=0 if event.get('measured_meaningful_progress',False) else s.no_progress_cycles+1
        return replace(s,state='await_fresh_observation',epoch=s.epoch+1,hold_confirmed_ns=now,no_progress_cycles=count),'invalidate_old_epoch_clear_prefix_after_hold'
    if s.state=='await_fresh_observation' and kind=='fresh_observation':
        if (len(event.get('camera_capture_started_ns',[]))!=3 or
            min(event['camera_capture_started_ns'])<=s.hold_confirmed_ns or
            event.get('state_feedback_ns',0)<=s.hold_confirmed_ns or
            not event.get('all_inputs_valid') or not event.get('bootstrap') or
            event.get('committed_prefix')!=[] or event.get('base_plan_id')!=0):
            return s,'reject_stale_or_nonbootstrap_observation'
        return replace(s,state='bootstrap_pending'),'request_new_model_from_actual_hold_no_old_context'
    if s.state=='bootstrap_pending' and kind=='bootstrap_result':
        if not event.get('source_identity_valid') or not event.get('full_ik_valid'):
            return replace(s,state='paused'),'failed_bootstrap_no_unbounded_retry_loop'
        return replace(s,state='running'),'resume_only_fresh_owned_full_plan'
    return s,'event_not_valid_for_state'


def validate_reducer():
    r=Recovery();checks={}
    rejection=dict(kind='verified_ik_rejection',valid_new=13,request_id=61,prefix_exact=True,before_original_adoption_deadline=True)
    a,why=transition(r,rejection);checks['thirteen_points_not_limited_by_next_model_deadline']=a.state=='await_stop_certificate'
    checks['zero_points_hold']=transition(r,rejection|{'valid_new':0})[0].state=='hold_required'
    checks['late_adoption_hold']=transition(r,rejection|{'before_original_adoption_deadline':False})[0].state=='hold_required'
    checks['prefix_changed_hold']=transition(r,rejection|{'prefix_exact':False})[0].state=='hold_required'
    checks['missing_certificate_hold']=transition(a,dict(kind='stop_certificate'))[0].state=='hold_required'
    synthetic=dict(kind='stop_certificate',**{k:True for k in ['actual_qva_fresh','matches_state_and_plan','immutable_prefix_times_exact','stays_within_valid_path','limits_and_wrist_valid','continuous_qva_and_bounded_jerk','gripper_phase_semantics_valid','stop_before_failed_target','fallback_stop_from_every_sample']})
    b,why=transition(a,synthetic);checks['synthetic_certificate_reaches_progress']=b.state=='progress_then_stop'
    checks['prediction_during_progress_not_adopted']=transition(b,dict(kind='bootstrap_result',full_ik_valid=True))[0]==b
    checks['owner_handoff_aborts']=transition(b,dict(kind='tick',owner='human'))[0].state=='handoff_abort'
    checks['not_settled_no_new_epoch']=transition(b,dict(kind='stop_confirmed',now_ns=100))[0].epoch==r.epoch
    c,why=transition(b,dict(kind='stop_confirmed',now_ns=100,measured_settled=True,measured_hold_committed=True,feedback_after_final_command=True,measured_meaningful_progress=False))
    checks['hold_advances_epoch_once']=c.epoch==r.epoch+1 and c.state=='await_fresh_observation'
    checks['old_reply_discarded']=transition(c,dict(kind='bootstrap_result',epoch=r.epoch,full_ik_valid=True))[1]=='stale_epoch_discarded'
    obs=dict(kind='fresh_observation',camera_capture_started_ns=[101,102,103],state_feedback_ns=102,all_inputs_valid=True,bootstrap=True,committed_prefix=[],base_plan_id=0)
    checks['stale_camera_rejected']=transition(c,obs|{'camera_capture_started_ns':[100,102,103]})[0]==c
    checks['old_prefix_rejected']=transition(c,obs|{'committed_prefix':[[0.]*16]})[0]==c
    d,_=transition(c,obs);checks['fresh_observation_unlocks_bootstrap']=d.state=='bootstrap_pending'
    e,_=transition(d,dict(kind='bootstrap_result',source_identity_valid=True,full_ik_valid=True));checks['fresh_owned_result_resumes']=e.state=='running'
    checks['cycle_budget_blocks']=transition(replace(r,cycles=3),rejection)[0].state=='paused'
    checks['two_no_progress_cycles_block']=transition(replace(r,no_progress_cycles=2),rejection)[0].state=='paused'
    checks['repeated_state_block']=transition(r,rejection|{'same_stalled_state':True})[0].state=='paused'
    assert all(checks.values()),checks
    return checks


def main():
    src=TASK/'Eval/artifacts/20261020-50k-ik/joined-requests.jsonl'
    rows=[json.loads(x) for x in src.read_text().splitlines()]
    sources=[src,Path(__file__),TASK/'src/f1_toys/native/kinematics.py',TASK/'src/f1_toys/native/ccs_m6_40.MvKDCfg']
    variants={}
    for bound,folder in [(-95,'results-j2-refinement'),(-96,'results'),(-97,'results-j2-refinement')]:
        p=TASK/f'Eval/artifacts/20261020-driver-range-study/{folder}/j2_minus{abs(bound)}_only.json';sources.append(p)
        variants[bound]={(r['episode'],r['request_id']):r for r in json.loads(p.read_text())['full_replays']}
    model=reference_model();records=[]
    for row in rows:
        raw=row['server']
        if raw['success']:continue
        key=(row['episode'],raw['request_id']);cases=[]
        for bound in [None,-95,-96,-97]:
            diags=raw['diagnostics'] if bound is None else variants[bound][key]['diagnostics']
            full=False if bound is None else variants[bound][key]['success']
            m=targets(raw,diags,model)
            cases.append(dict(j2_lower_candidate=bound,full_ik=full,**m,
                route='ordinary_full_plan' if full else ('hold_without_partial_progress' if m['valid_new_points']==0 else 'requires_dynamic_stop_certificate')))
        records.append(dict(episode=key[0],request_id=key[1],bootstrap=raw['rtc_request']['bootstrap'],cases=cases))
    summary=[]
    for bound in [None,-95,-96,-97]:
        cases=[(r,next(c for c in r['cases'] if c['j2_lower_candidate']==bound)) for r in records]
        partial=[(r,c) for r,c in cases if not c['full_ik'] and c['valid_new_points']>0]
        zeros=[r for r,c in cases if not c['full_ik'] and c['valid_new_points']==0]
        summary.append(dict(j2_lower_candidate=bound,requests=49,complete_ik=sum(c['full_ik'] for _,c in cases),
            partial_nonzero=len(partial),partial_zero=len(zeros),
            zero_ids=[dict(episode=r['episode'],request_id=r['request_id']) for r in zeros],
            partial_points_min=min((c['valid_new_points'] for _,c in partial),default=0),
            partial_points_max=max((c['valid_new_points'] for _,c in partial),default=0),
            partial_endpoint_translation_min_mm=min((c['endpoint_translation_mm'] for _,c in partial),default=0),
            partial_endpoint_translation_max_mm=max((c['endpoint_translation_mm'] for _,c in partial),default=0),
            illustrative_endpoint_translation_counts={str(t):sum(c['endpoint_translation_mm']>=t for _,c in partial) for t in [1,2,5,10]},
            illustrative_excursion_translation_counts={str(t):sum(c['max_excursion_translation_mm']>=t for _,c in partial) for t in [1,2,5,10]},
            planned_gripper_half_crossings=sum(c['planned_gripper_crosses_half'] for _,c in partial),
            dynamic_stop_certified_count=0,actual_recovery_success_count=0))
    output=dict(schema='progress_stop_reobserve_prototype_v1',date='2026-10-21',summary=summary,records=records,
        reducer_validation=validate_reducer(),
        semantics=[
            'Current recovery immediately drops all valid tentative suffix points and HOLDs. This candidate can execute a certified finite contiguous valid suffix, stop within it, confirm measured HOLD, then reobserve/bootstrap.',
            'No next model must finish before the finite path ends. This removes the D<=S inference-budget obstruction for13-point request61, but does not remove dynamic braking or initial adoption deadlines.',
            'Already committed prefix values and wall-time schedule remain unchanged during planned recovery. If no legal brake can start after that boundary and stay in the valid segment, do not adopt partial progress.',
            'Stopping may end before the last valid model waypoint on the same existing path. No target beyond the first failed waypoint, geometric projection, invented waypoint or gripper reversal is permitted by this prototype.',
            'Keep one existing output owner. Freeze new inference while the finite segment executes; isolate/cancel competing result adoption without transferring control. At confirmedHOLD invalidate oldepoch and clearprefix, then require allthree new camera captures and actual state after settling.',
            'Nextbootstrap may still fail. Preserve bounded cycle/no-progress history across successful bootstrap transitions rather than resetting it every time. Production thresholds must be task-calibrated; illustrative3-cycle/2-no-progress reducer checks are not enabled defaults.',
            'Gripper changes and insertion/contact may make a stop semantically inappropriate. Endpoint displacement is geometric opportunity, not task progress or proof that moving closer to the failed boundary is beneficial.'
        ],
        required_stop_certificate=[
            'Fresh measured/submitted/output q,v,a and tracking discrepancy plus correct owner/epoch/plan/time; certificate invalidated on state drift beyond verified bound.',
            'Exact immutable prefix values and timestamps retained; actual starting q,v,a can connect continuously to a time-scaled valid joint path after prefix boundary.',
            'Continuous q/v/a and boundedjerk, joint/wrist bounds and output collision/clearance requirements at interpolated trajectory points, not just IK waypoints.',
            'A complete deceleration-to-zero trajectory lies within the finite verified path and ends strictly before the first failed target; no saturation/clipping used as hidden target repair.',
            'A valid emergency stop remains available from every execution sample, including while finishing immutable prefix, using existing safety owner if normal commitment cannot be preserved.',
            'Gripper timing shares path phase or is explicitly justified for current task/contact; no artificial open/close or reverse playback.',
            'Settling confirmation uses actual feedback after finalcommand and measured velocity/acceleration tolerances, not mere duration or a command publication acknowledgment.',
            'No certificate generated by this audit; existing Ruckig output is not presumed to satisfy path-preserving finite-stop certification.'
        ],
        no_progress_contract=[
            'Track cumulative recovery count and elapsed recovery time from the original rejection until meaningful task progress, including each newbootstrap; no unbounded repeat.',
            'Measure actual displacement/rotation and task state after HOLD, not planned waypoint distance. Combine a small-motion floor with repeated-state or recurring-IK boundary detection.',
            'Tiny forward/back oscillations must not reset progress counters; compare net displacement and task-phase evidence to the start of the recovery episode.',
            'Gripper toggle alone is not progress. A release or insertion-contact transition requires task semantics and may disallow this recovery.',
            'On zero newpoints, missing certificate, staleowner, no progress or exhaustedbudget, retain existing HOLD/DAgger/manual recovery path.'
        ],
        limitations=[
            'All49 frozen rejected contexts; candidate prefixes reset perrequest. No full continuous candidate rollout, newobservation or modelpredict.',
            'ReferenceFK reuses exact ArmModel AST from pinned source; only unrelated scipy imports are excluded. All valid point targets checked against original .01mm/.001deg gate.',
            'Saved IK points carry target q only; dense actual q/v/a and contact evidence needed for stopping are absent. Every dynamic_stop_certified_count is0.',
            'Displacement starts from last immutable target in guided windows or measured requeststate in bootstrap; it excludes oldcommitted motion and is not necessarily actual position at response.',
            'Expanded full-window success is separate from partial opportunity; no49/49 recovery success or probability claimed.',
            'J2 fallback bounds remain hardware-unapproved; no driver, currentservice, GPU or motion changes.'
        ],model_calls=0,robot_commands=0,production_changes=0)
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/'progress-hold-results.json').write_text(json.dumps(output,indent=2)+'\n')
    (OUT/'progress-hold-summary.json').write_text(json.dumps({k:v for k,v in output.items() if k!='records'},indent=2)+'\n')
    (OUT/'progress-hold-manifest.json').write_text(json.dumps(dict(sources=[dict(path=str(p),sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in sources]),indent=2)+'\n')
    print(json.dumps(summary,indent=2))
    print('state checks:',len(output['reducer_validation']))
if __name__=='__main__':main()

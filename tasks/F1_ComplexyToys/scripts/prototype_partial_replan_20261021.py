#!/usr/bin/env python3
"""Offline pure scheduler prototype: no model, robot, transport or production mutation.

A partial response never acquires success=True. Complete windows retain their
original semantics. Partial acceptance needs a new explicitly negotiated protocol.
No output of this program is an executable robot command.
"""
from dataclasses import dataclass, asdict
from pathlib import Path
import collections
import hashlib
import json
import math

TASK = Path(__file__).resolve().parents[1]
OUT = TASK / 'Eval/artifacts/20261021-ik-recovery-next/partial'
TICK_NS = 1_000_000_000 / 30

@dataclass(frozen=True)
class Proposal:
    status: str
    reason: str
    valid_count: int
    new_valid_count: int
    origin_ns: int
    adopt_ns: int
    next_delay: int = 0
    next_shift: int = 0
    query_ns: int = 0
    reply_budget_deadline_ns: int = 0
    prefix_deadline_ns: int = 0
    last_valid_point_ns: int = 0
    stop_start_deadline_ns: int = 0
    immutable_next_prefix: tuple = ()


def schedule_partial(*, actions, original_prefix, origin_ns, reply_ns,
                     bootstrap, original_delay, next_delay, next_latency_ms,
                     observation_wait_ms=33.334, stop_reserve_ms=66.667,
                     owner_valid=True, epoch_valid=True):
    """Preserve D<=S, time grid and exact prefix; do not manufacture a prediction.

    Conservative endpoint is point (L-1), one tick before the production sampler
    exhausts L points. stop_reserve_ms is a *parameter requiring certified braking*,
    not a proven hardware stopping bound. Caller must stop by stop_start_deadline_ns.
    Fresh observation is requested after adoption; wait is a budget assumption.
    """
    n, p = len(actions), len(original_prefix)
    origin = reply_ns if bootstrap else origin_ns
    def reject(reason):
        return Proposal('hold_required', reason, n, n-p, origin, reply_ns)
    if not owner_valid or not epoch_valid:
        return reject('stale_owner_or_epoch')
    if any(len(x) != 16 or any(not math.isfinite(v) for v in x) for x in actions):
        return reject('malformed_actions')
    if tuple(tuple(x) for x in actions[:p]) != tuple(tuple(x) for x in original_prefix):
        return reject('committed_prefix_changed')
    if n <= p:
        return reject('no_valid_new_target')
    if not bootstrap and reply_ns >= origin + round(original_delay*TICK_NS):
        return reject('original_adoption_deadline')
    if not 1 <= next_delay < 25:
        return reject('invalid_next_delay')
    # earliest available genuinely new image, and retain RTC d <= s invariant.
    earliest = max(reply_ns + math.ceil(observation_wait_ms*1e6),
                   origin + round(next_delay*TICK_NS))
    shift = max(next_delay, math.floor((earliest-origin)/TICK_NS))
    query = earliest
    if shift >= 50-next_delay:
        return reject('next_horizon_contract')
    if shift + next_delay > n:
        return reject('insufficient_immutable_prefix')
    predicted_reply = query + math.ceil(next_latency_ms*1e6)
    prefix_deadline = origin + round((shift+next_delay)*TICK_NS)
    last_point = origin + round((n-1)*TICK_NS)
    stop_start = last_point - math.ceil(stop_reserve_ms*1e6)
    if predicted_reply >= prefix_deadline:
        return reject('next_reply_misses_committed_prefix')
    if predicted_reply > stop_start:
        return reject('insufficient_stop_reserve')
    return Proposal('time_opportunity_only', 'await_new_observation_and_new_prediction', n, n-p,
                    origin, reply_ns, next_delay, shift, query, predicted_reply,
                    prefix_deadline, last_point, stop_start,
                    tuple(tuple(x) for x in actions[shift:shift+next_delay]))


def candidate_actions(raw, diagnostics):
    prefix = raw['rtc_request']['committed_actions16']
    actions = [list(x) for x in prefix]
    for index, d in enumerate(diagnostics):
        if not d['success']:
            assert index == len(diagnostics)-1
            break
        assert d['position_error_mm'] <= .01 and d['rotation_error_deg'] <= .001
        a = list(raw['prediction_replay']['last_submitted16'])
        a[7:14] = d['joints']
        a[15] = min(100., max(0., raw['absolute_eef10'][len(actions)][9]*50+50))
        actions.append(a)
    assert actions[:len(prefix)] == prefix
    return actions


def boundary_checks():
    q = [[float(i)]*16 for i in range(42)]
    base = dict(actions=q, original_prefix=q[:20], origin_ns=0,
                reply_ns=300_000_000, bootstrap=False, original_delay=20,
                next_delay=14,next_latency_ms=400.,observation_wait_ms=33.334,
                stop_reserve_ms=66.667)
    checks = {}
    good = schedule_partial(**base)
    checks['normal_schedule'] = good.status == 'time_opportunity_only'
    checks['d_le_s'] = good.next_shift >= good.next_delay
    checks['exact_prefix'] = good.immutable_next_prefix == tuple(tuple(x) for x in q[14:28])
    for name, kwargs, reason in [
        ('late_original',dict(reply_ns=666_666_667),'original_adoption_deadline'),
        ('stale_epoch',dict(epoch_valid=False),'stale_owner_or_epoch'),
        ('wrong_owner',dict(owner_valid=False),'stale_owner_or_epoch'),
        ('nonfinite',dict(actions=q[:-1]+[[math.nan]*16]),'malformed_actions'),
        ('empty_suffix',dict(actions=q[:20]),'no_valid_new_target'),
        ('short_prefix',dict(actions=q[:27]),'insufficient_immutable_prefix'),
        ('late_next_reply',dict(next_latency_ms=500),'next_reply_misses_committed_prefix'),
        ('braking_budget',dict(actions=q[:29],stop_reserve_ms=150),'insufficient_stop_reserve'),
        ('prefix_tamper',dict(original_prefix=[[999.]*16]+q[1:20]),'committed_prefix_changed')]:
        checks[name] = schedule_partial(**(base|kwargs)).reason == reason
    checks['all_pass'] = all(checks.values())
    assert checks['all_pass'], checks
    return checks


def sample_grid(actions, anchor, u):
    """Same piecewise-linear anchor/waypoint convention as production sample()."""
    if u < 1:
        return [(1-u)*a+u*b for a,b in zip(anchor,actions[0])]
    index=math.floor(u)-1
    fraction=u-math.floor(u)
    return [(1-fraction)*a+fraction*b for a,b in zip(actions[index],actions[index+1])]


def check_rebased_prefix(actions, shift, delay):
    # At the new origin the anchor is old action[S-1], not old action[S].
    # Compare noninteger times throughout the immutable interval, including seam.
    prefix=actions[shift:shift+delay]
    error=0.
    for i in range(delay*7):
        u=i/7
        a=sample_grid(actions,actions[0],shift+u)
        b=sample_grid(prefix,actions[shift-1],u)
        error=max(error,max(abs(x-y) for x,y in zip(a,b)))
    assert error < 1e-10
    return error


def main():
    source=TASK/'Eval/artifacts/20261020-50k-ik/joined-requests.jsonl'
    rows=[json.loads(x) for x in source.read_text().splitlines()]
    sources=[source,Path(__file__)]
    range_root=TASK/'Eval/artifacts/20261020-driver-range-study'
    alternatives={}
    for bound,folder in [(-95,'results-j2-refinement'),(-96,'results'),(-97,'results-j2-refinement')]:
        path=range_root/folder/f'j2_minus{abs(bound)}_only.json'
        sources.append(path)
        alternatives[bound]={(r['episode'],r['request_id']):r for r in json.loads(path.read_text())['full_replays']}
    profiles=[dict(name=f'd{d}_latency{latency}_stop{stop}',next_delay=d,next_latency_ms=latency,
                   stop_reserve_ms=stop) for d,latency in [(20,400),(14,400),(12,300),(16,467)]
              for stop in [66.667,150,300]]
    records=[]
    seams_checked=0
    max_seam_error=0.
    for row in rows:
        raw=row['server']; key=(row['episode'],raw['request_id']); context=raw['rtc_request']
        original=candidate_actions(raw,raw['diagnostics'])
        if raw['success']:
            # Numerical gripper conversion can differ in last bit; original return is retained.
            assert len(original)==context['coverage_steps']
            records.append(dict(episode=key[0],request_id=key[1],original_success=True,
                                path='strict_original_unchanged', returned_actions_sha256=hashlib.sha256(
                                    json.dumps(raw['actions']).encode()).hexdigest()))
            continue
        cases=[]
        for bound in [None,-95,-96,-97]:
            branch=original; full=False; branch_name='original_strict'
            if bound is not None:
                alternative=alternatives[bound][key]
                expanded=candidate_actions(raw,alternative['diagnostics'])
                full=alternative['success']
                if len(expanded)>=len(original):
                    branch=expanded;branch_name='expanded_whole_suffix'
            for extra_ms in ([0] if bound is None else [0,40,80]):
                reply=row['client_timing']['monotonic_ns']+round(extra_ms*1e6)
                prefix=context['committed_actions16']
                adoption_valid=context['bootstrap'] or reply<context['plan_origin_ns']+round(context['delay_steps']*TICK_NS)
                plans=[]
                if not full:
                    for profile in profiles:
                        kwargs={k:v for k,v in profile.items() if k!='name'}
                        proposal=schedule_partial(actions=branch,original_prefix=prefix,origin_ns=context['plan_origin_ns'],
                            reply_ns=reply,bootstrap=context['bootstrap'],original_delay=context['delay_steps'],**kwargs)
                        data=asdict(proposal)
                        # Prefix exactness is checked above and here; retain hash rather than copied q arrays.
                        if proposal.status=='time_opportunity_only':
                            assert list(map(list,proposal.immutable_next_prefix))==branch[proposal.next_shift:proposal.next_shift+proposal.next_delay]
                            max_seam_error=max(max_seam_error,check_rebased_prefix(branch,proposal.next_shift,proposal.next_delay))
                            seams_checked+=1
                        data['immutable_next_prefix_sha256']=hashlib.sha256(json.dumps(data.pop('immutable_next_prefix')).encode()).hexdigest()
                        plans.append(dict(profile=profile['name'],**data))
                cases.append(dict(j2_lower_candidate=bound,assumed_additional_decode_ms=extra_ms,
                    branch=branch_name,valid_total=len(branch),valid_new=len(branch)-len(prefix),
                    full_window_ik_pass=full,within_original_adoption_deadline=adoption_valid,plans=plans))
        records.append(dict(episode=key[0],request_id=key[1],original_success=False,bootstrap=context['bootstrap'],cases=cases))
    summary=[]
    for bound in [None,-95,-96,-97]:
        for extra in ([0] if bound is None else [0,40,80]):
            cases=[(r,c) for r in records if not r['original_success'] for c in r['cases']
                   if c['j2_lower_candidate']==bound and c['assumed_additional_decode_ms']==extra]
            for profile in profiles:
                opportunities=[r for r,c in cases for p in c['plans'] if p['profile']==profile['name'] and p['status']=='time_opportunity_only']
                summary.append(dict(j2_lower_candidate=bound,additional_decode_ms=extra,profile=profile['name'],
                    full_ik_and_adoption=sum(c['full_window_ik_pass'] and c['within_original_adoption_deadline'] for _,c in cases),
                    partial_time_opportunities=len(opportunities),partial_ids=[dict(episode=r['episode'],request_id=r['request_id']) for r in opportunities],
                    hold_reasons=dict(collections.Counter(p['reason'] for _,c in cases for p in c['plans']
                        if p['profile']==profile['name'] and p['status']=='hold_required'))))
    output=dict(schema='offline_partial_rtc_contract_prototype_v1',date='2026-10-21',requests=len(rows),
        successes_preserved=sum(r['original_success'] for r in records),rejected_cases=sum(not r['original_success'] for r in records),
        boundary_checks=boundary_checks(),rebased_prefix_interpolation_checks=seams_checked,
        max_16d_interpolation_difference=max_seam_error,profiles=profiles,summary=summary,records=records,
        integration_points=[
            'Server decode_window: explicit partial status with only contiguous valid actions, diagnostics, first failure index, full original absolute EEF, applied limits identity. Never return partial as success.',
            'RTC transport: negotiate new protocol; validate finite valid length, immutable prefix, action limits and exact source identity. Existing v2 rejects partial and must remain fail closed.',
            'RTC scheduler: retain original plan clock/anchor and owner/epoch, adopt only before original hard-prefix deadline, choose earlier legal S>=D based on measured latency and actual available observation; do not backdate requests or play failed points.',
            'Next context: committed_actions16 is exact accepted partial slice[S:S+D], previous absolute EEF tail unchanged; preserve current RTC soft_mask D<=S. Adaptive D reduction needs separate end-to-end latency qualification.',
            'Output owner: certify available stopping trajectory from current position/velocity/acceleration and stop before last valid point if next reply absent or invalid; this prototype uses assumed stopping budgets only.',
            'Bound repeated partials/no progress and reuse existing HOLD/DAgger ownership; no physical rollback or gripper reversal here.'],
        caveats=[
            'Time opportunities are necessary scheduling conditions, never rescued windows or task success.',
            'All candidate windows use recorded old 50K prefix/observations. No candidate-own-prefix rollout or counterfactual next model output exists.',
            'Expanded J2 remains unapproved for hardware; this program does not alter production limits.',
            'Extra decode latency 0/40/80ms is sensitivity, not a platform guarantee; 40ms is near previous single CPU J2-97 retry max38.2ms, not a measured J2-95/96 bound.',
            'Fresh-image wait33.334ms and latency/stop reserve are explicit budget assumptions; real frame/transport/output latency must be measured.',
            'Historical transport-validation event time is used as earliest adoption time; scheduler dispatch lag is not measured here and must be added before deployment.',
            'No speed reduction, resampling, target projection, EEF edits or dropped intermediate valid points.',
            'D20 profile retains historical delay minimum; D14/D12/D16 are hypothetical settings with existing soft-mask math, not current old-50K configurations.',
            'Original IK diagnostics were independently FK-verified in prior audits; this audit checks gate fields and prefix protocol, does not rerun the optimizer.'],
        robot_commands=0,model_calls=0,deployment_changes=0)
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/'results.json').write_text(json.dumps(output,indent=2)+'\n')
    (OUT/'summary.json').write_text(json.dumps({k:v for k,v in output.items() if k!='records'},indent=2)+'\n')
    (OUT/'validation.json').write_text(json.dumps(dict(boundary_checks=output['boundary_checks'],
        rebased_prefix_interpolation_checks=seams_checked,max_16d_interpolation_difference=max_seam_error,
        original_success_paths_preserved=output['successes_preserved'],
        no_real_replanning_or_robot_calls=True),indent=2)+'\n')
    sources.extend(TASK/p for p in ['src/f1_toys/rtc.py','src/f1_toys/server.py'])
    sources.extend(TASK.parents[1]/p for p in ['DAgger/F1/src/f1dagger/rtc.py','DAgger/F1/src/f1dagger/rtc_policy.py'])
    sources.append(TASK.parents[1]/'DAgger/shared/src/dagger_core/powered_hil_policy.py')
    (OUT/'manifest.json').write_text(json.dumps(dict(sources=[dict(path=str(p),sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in sources]),indent=2)+'\n')
    for x in summary:
        if x['additional_decode_ms'] in [0,40] and x['profile'] in ['d20_latency400_stop66.667','d14_latency400_stop66.667','d12_latency300_stop66.667']:
            print(json.dumps({k:v for k,v in x.items() if k!='partial_ids'}))

if __name__=='__main__':main()

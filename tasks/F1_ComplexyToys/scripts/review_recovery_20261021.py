#!/usr/bin/env python3
"""Read-only historical recovery/scheduling audit; no robot/model imports."""
import collections
import hashlib
import json
import math
from pathlib import Path
import statistics

TASK = Path(__file__).resolve().parents[1]
SOURCE = TASK / 'Eval/artifacts/20261020-50k-ik'
OUT = TASK / 'Eval/artifacts/20261021-independent-ik/recovery'


def readlines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def margins(q):
    return {'j2_lower_deg': q[1] + 94,
            'wrist_deg': 110.5 - 1.025 * abs(q[5]) - abs(q[6])}


def main():
    rows = readlines(SOURCE / 'joined-requests.jsonl')
    grouped = collections.defaultdict(list)
    for row in rows:
        grouped[row['episode']].append(row)
    sources = [SOURCE / 'joined-requests.jsonl', Path(__file__)]
    streaks, timing, checkpoints, inventory = [], [], [], []
    for episode, group in sorted(grouped.items()):
        group.sort(key=lambda r: r['server']['request_id'])
        journal = SOURCE / 'f1-journals' / episode / 'journal'
        sources.extend([journal / 'events.jsonl', journal / 'manifest.json'])
        events = readlines(journal / 'events.jsonl')
        manifest = json.loads((journal / 'manifest.json').read_text())
        requests = {e['request_id']: e for e in events if e['event'] == 'powered_policy_request'}
        inventory.append(dict(episode=episode, original_manifest_counts=manifest['counts'],
                              locally_available_journal_files=sorted(p.name for p in journal.iterdir()),
                              measured_request_snapshots=len(group)))
        for row in group:
            s = row['server']
            if s['success']:
                continue
            context = s['rtc_request']
            diagnostics = s['diagnostics']
            assert diagnostics and not diagnostics[-1]['success']
            assert all(d['success'] for d in diagnostics[:-1])
            new_points = len(diagnostics) - 1
            valid_total = len(context['committed_actions16']) + new_points
            response_ns = row['client_timing']['monotonic_ns']
            # Bootstrap has no active trajectory origin: optimistic adoption at reply.
            origin_ns = response_ns if context['bootstrap'] else context['plan_origin_ns']
            # Conservative usable endpoint is final valid point, not the next sample.
            remaining_ms = (origin_ns - response_ns) / 1e6 + max(0, valid_total - 1) * 1000 / 30
            timing.append(dict(episode=episode, request_id=s['request_id'],
                               bootstrap=context['bootstrap'], valid_new_points=new_points,
                               committed_count=len(context['committed_actions16']),
                               valid_total=valid_total, response_monotonic_ns=response_ns,
                               assumed_origin_ns=origin_ns, usable_endpoint_remaining_ms=remaining_ms,
                               response_elapsed_from_origin_ms=(response_ns-origin_ns)/1e6,
                               recorded_client_total_ms=row['client_timing']['timing']['client_total_ms'],
                               fixed_d20_s20_two_guard_pass=valid_total >= 42,
                               immediate_requery_budget_pass={str(b): new_points > 0 and remaining_ms >= b + 2000 / 30
                                                             for b in [250, 300, 350, 400, 467, 600, 667]}))
        i = 0
        while i < len(group):
            if group[i]['server']['success']:
                i += 1
                continue
            start = i
            while i < len(group) and not group[i]['server']['success']:
                i += 1
            failed = group[start:i]
            success = group[i] if i < len(group) else None
            lo = requests[failed[0]['server']['request_id']]['monotonic_ns']
            hi = success['client_timing']['monotonic_ns'] if success else math.inf
            related = [e for e in events if lo <= e['monotonic_ns'] <= hi]
            resume_events = [e for e in related if e['event'] == 'ik_recovery_resumed']
            followups = failed[1:] + ([success] if success else [])
            checks = []
            for a, b in zip(failed, followups):
                aa, bb = a['server'], b['server']
                holds = [e for e in related if e['event'] == 'ik_recovery_hold_committed'
                         and a['client_timing']['monotonic_ns'] < e['monotonic_ns']
                         < requests[bb['request_id']]['monotonic_ns']]
                checks.append(dict(from_request=aa['request_id'], to_request=bb['request_id'],
                    fresh_observation=bb['observation_monotonic_ns'] > aa['observation_monotonic_ns'],
                    old_prefix_cleared=not bb['rtc_request']['committed_actions16'] and bb['rtc_request']['bootstrap'],
                    epoch_advanced=bb['policy_epoch'] > aa['policy_epoch'],
                    hold_committed_before_next_request=bool(holds),
                    observation_after_hold=bool(holds) and bb['observation_monotonic_ns'] > holds[-1]['monotonic_ns']))
            first = failed[0]['server']
            last = success['server'] if success else failed[-1]['server']
            q0, q1 = [x['prediction_replay']['state16'][7:14] for x in [first, last]]
            streaks.append(dict(episode=episode, rejected_ids=[r['server']['request_id'] for r in failed],
                next_success_id=success['server']['request_id'] if success else None,
                enter_resumes_before_next_success=len(resume_events),
                outcome='still_rejected_at_end' if not success else ('after_enter_and_retries' if resume_events else 'automatic_retry_no_enter_recorded'),
                elapsed_observation_s=(last['observation_monotonic_ns']-first['observation_monotonic_ns'])/1e9,
                measured_endpoint_joint_delta_max_deg=max(abs(a-b) for a,b in zip(q0,q1)),
                subsequent_successful_requests_until_next_reject=next((j-i for j in range(i,len(group)) if not group[j]['server']['success']),len(group)-i) if success else 0,
                followup_checks=checks))
        failed_indices = [i for i, r in enumerate(group) if not r['server']['success']]
        if failed_indices:
            index = failed_indices[0]
            current = group[index]['server']
            q = current['prediction_replay']['state16'][7:14]
            candidates = []
            for j, row in enumerate(group[:index]):
                old = row['server']
                age = (current['observation_monotonic_ns']-old['observation_monotonic_ns'])/1e9
                if age > 10:
                    continue
                oq = old['prediction_replay']['state16'][7:14]
                grips = [x['server']['prediction_replay']['state16'][15] for x in group[j:index+1]]
                gaps = [(b['server']['observation_monotonic_ns']-a['server']['observation_monotonic_ns'])/1e9
                        for a,b in zip(group[j:index],group[j+1:index+1])]
                candidates.append(dict(request_id=old['request_id'], age_s=age,
                    measured_joints=oq, margins=margins(oq),
                    endpoint_max_joint_delta_deg=max(abs(a-b) for a,b in zip(q,oq)),
                    sparse_observed_gripper_range=[min(grips),max(grips)],
                    sparse_gripper_crosses_half=min(grips)<50<=max(grips),
                    max_measured_snapshot_gap_s=max(gaps) if gaps else 0,
                    illustrative_margin_screen=oq[1]+94>=1 and margins(oq)['wrist_deg']>=3))
            checkpoints.append(dict(episode=episode, first_reject_id=current['request_id'],
                current_margins=margins(q), candidates=candidates,
                time_window_screens={str(t):dict(total=sum(c['age_s']<=t for c in candidates),
                    margin_and_gripper_screen_count=sum(c['age_s']<=t and c['illustrative_margin_screen']
                        and not c['sparse_gripper_crosses_half'] for c in candidates)) for t in [2,5,10]}))
    all_latencies = sorted(r['client_timing']['timing']['client_total_ms'] for r in rows)
    result = dict(schema='f1_independent_recovery_review_v1', source_scope='Six old 50K D20/S20 powered episodes; no Full625/1K D14/S16 replay.',
        timing_formula='remaining_ms = (origin_ns - response_ns)/1e6 + (valid_total-1)*1000/30; eligible iff valid_new_points>0 and remaining_ms >= assumed_next_latency_ms + 2*1000/30. Bootstrap origin is hypothetical reply/adoption time.',
        summary=dict(requests=len(rows), rejected=len(timing), rejection_streaks=len(streaks),
            recorded_all_client_latency_ms=dict(p50=statistics.median(all_latencies),p95_nearest_rank=all_latencies[math.ceil(.95*len(all_latencies))-1],max=max(all_latencies)),
            recovery_outcomes=dict(collections.Counter(s['outcome'] for s in streaks)),
            all_followups_fresh_clear_prefix_advance_epoch_hold=all(all(c[k] for k in ['fresh_observation','old_prefix_cleared','epoch_advanced','hold_committed_before_next_request','observation_after_hold']) for s in streaks for c in s['followup_checks']),
            immediate_requery_necessary_time_screen={str(b):dict(total=sum(t['immediate_requery_budget_pass'][str(b)] for t in timing),
                nonbootstrap=sum(t['immediate_requery_budget_pass'][str(b)] and not t['bootstrap'] for t in timing),
                bootstrap=sum(t['immediate_requery_budget_pass'][str(b)] and t['bootstrap'] for t in timing)) for b in [250,300,350,400,467,600,667]}),
        rejection_streaks=streaks, partial_schedule=timing, time_based_measured_checkpoints=checkpoints,
        data_inventory=inventory,
        caveats=[
            'Automatic retry label denotes logged control flow, not causal proof that prefix clearing or observation alone caused recovery.',
            'No historical rollback was executed. No return-path/contact/object-state certification or new model prediction.',
            'Partial scheduling screen assumes immediate requery, conservative last valid-point endpoint, plus 2 ticks; this violates existing fixed-shift protocol and requires a new scheduler/continuity proof.',
            'Latency budgets are sensitivity assumptions, not guarantees or current Full service measurements. No hypothetical next observation/target was fabricated.',
            'Bootstrap adopts its feasible prefix at reply only in the hypothetical screen; original rejected chunks were never executed.',
            'Sparse request snapshots are measured, but returned actions and diagnostics are planned; original manifest lists dense control/observations that are absent in this local snapshot.',
            'J2 1 degree/wrist 3 degree and gripper 50 are diagnostic screens, not accepted hardware/task/safety thresholds.',
            'No gripper crossing in sparse observations does not establish unchanged grasp, object position, insertion depth or contact.'])
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT/'results.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
    (OUT/'manifest.json').write_text(json.dumps(dict(robot_commands=0,model_calls=0,deployment_changes=0,
        sources=[dict(path=str(p.relative_to(TASK)),sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in sources]),indent=2)+'\n')
    print(json.dumps(result['summary'],indent=2))


if __name__ == '__main__':
    main()

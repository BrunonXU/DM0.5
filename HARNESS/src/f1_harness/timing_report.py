"""Phase timing from one clock per interval; incomplete runs are not cycle benchmarks."""
import json
import math
from pathlib import Path
from .startup_report import owner_startup_report


# Claude's 20 s deployment target allocates 18 s to placement and leaves 2 s spare.
STAGE_BUDGET_S = {
    'start_to_first_publication': 2.5,
    'approach_execution': 4.0,
    'grasp_and_settle': 0.8,
    'grasp_verification': 0.7,
    'transport': 5.0,
    'alignment': 2.5,
    'insert_release_verify': 2.5,
}
_RESULT_STAGE = {
    'approach_grasp': 'approach_execution',
    'grasp': 'grasp_and_settle', 'regrasp': 'grasp_and_settle',
    'verify_grasp': 'grasp_verification',
    'transfer_above_hole': 'transport', 'align': 'alignment',
    'verify_placed': 'insert_release_verify',
}


def _elapsed(start, end):
    return (end - start) / 1e9 if type(start) is int and type(end) is int and end >= start else None


def _budget_checkpoint(row):
    if row.get('kind') == 'harness_skill_first_publication':
        return 'start_to_first_publication', row.get('published_ns')
    if row.get('kind') == 'skill_result' and row.get('result', {}).get('outcome') == 'success':
        return _RESULT_STAGE.get(row.get('skill')), row.get('monotonic_ns')
    return None, None


def _budget_comparison(actual, budget):
    return dict(actual_s=actual, over_budget_s=max(0.0, actual - budget) if actual is not None else None)


def _budget_report(owner, planner, started_ns, planner_available):
    stages = list(STAGE_BUDGET_S)
    owner_ends = {}
    for row in owner:
        stage, stamp = _budget_checkpoint(row)
        if stage and type(stamp) is int:
            owner_ends.setdefault(stage, stamp)

    # Attribute each logical model call to the phase observed by the planner when it began.
    # Arrival of mirrored milestones and all model events use the same planner clock.
    next_stage = 0
    planner_ends = {}
    requests = {}
    unattributed_calls = []
    for row in planner:
        kind = row['kind']
        request = row.get('request_id')
        if kind == 'owner_event':
            stage, _ = _budget_checkpoint(row.get('record') or {})
            if stage and type(row.get('monotonic_ns')) is int:
                planner_ends.setdefault(stage, row['monotonic_ns'])
                next_stage = max(next_stage, stages.index(stage) + 1)
        elif kind == 'model_request_started':
            stage = stages[next_stage] if next_stage < len(stages) else 'after_placement'
            if not request or request in requests:
                unattributed_calls.append(stage)
            else:
                requests[request] = dict(stage=stage, started_ns=row.get('monotonic_ns'), wait_s=None)
        elif (kind in ('model_response', 'model_rejected')
              or kind == 'model_transport_error' and not row.get('automatic_retry')):
            if request not in requests:
                unattributed_calls.append('unknown')
                continue
            elapsed = row.get('elapsed_s')
            if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
                elapsed = _elapsed(requests[request]['started_ns'], row.get('monotonic_ns'))
            requests[request]['wait_s'] = elapsed

    def model_metrics(stage=None):
        calls = [item for item in requests.values() if stage is None or item['stage'] == stage]
        unknown = any(stage is None or missing in (stage, 'unknown') for missing in unattributed_calls)
        known = planner_available and not unknown
        waits = [item['wait_s'] for item in calls if item['wait_s'] is not None]
        return dict(model_calls=len(calls) if known else None,
                    model_wait_s=sum(waits) if known and len(waits) == len(calls) else None)

    phases = {}
    for i, stage in enumerate(stages):
        if i == 0:
            actual = _elapsed(started_ns, planner_ends.get(stage))
        else:
            actual = _elapsed(owner_ends.get(stages[i - 1]), owner_ends.get(stage))
        phases[stage] = {**_budget_comparison(actual, STAGE_BUDGET_S[stage]), **model_metrics(stage)}
    placed = _elapsed(started_ns, planner_ends.get(stages[-1]))
    return dict(stage_budget_s=dict(STAGE_BUDGET_S), stage_timing=phases,
                placement_target=dict(allocated_s=sum(STAGE_BUDGET_S.values()), target_s=20.0,
                                      **_budget_comparison(placed, 20.0)),
                model_timing=model_metrics(), model_timing_after_placement=model_metrics('after_placement'))


def _rows(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.is_file() else []



def measured_cycle(owner,accepted):
    """Only a completed same-scope placement and return can define cycle time."""
    from .evidence_pack import motion_evidence_request
    intents={k:v['intent'] for k,v in accepted.items()}
    required={'approach_grasp','grasp','verify_grasp','inspect_in_hand','transfer_above_hole',
              'align','insert','release','verify_placed','withdraw_reset'}
    terminal=[r for r in owner if r.get('kind')=='skill_result' and r.get('skill')=='withdraw_reset'
              and r['result'].get('outcome')=='success' and r['result'].get('synthetic') is False
              and r['result'].get('evidence_ids')]
    for reset in reversed(terminal):
        request=reset['result']['request_id'];scope=intents.get(request)
        if not scope:continue
        keys=('task_hash','session_id','owner_instance_id','epoch')
        if any(scope.get(k) is None for k in keys):continue
        ids={k for k,i in intents.items() if all(i.get(f)==scope[f] for f in keys)}
        results=[r for r in owner if r.get('kind')=='skill_result'
            and r['result'].get('request_id') in ids and r['result'].get('outcome')=='success'
            and r['result'].get('synthetic') is False and r['result'].get('evidence_ids')
            and r['monotonic_ns']<=reset['monotonic_ns']]
        if not required<={r['skill'] for r in results}:continue
        if any(r['result'].get('assisted') for r in results):continue
        placed=max(r['monotonic_ns'] for r in results if r['skill']=='verify_placed')
        if placed>accepted[request]['monotonic_ns']:continue
        motion=motion_evidence_request(request,intents,owner)
        ends=[r['measured_stamp_ns'] for r in owner if r.get('kind')=='harness_measured_waypoint'
              and r.get('request_id')==motion and r['measured_stamp_ns']<=reset['monotonic_ns']]
        starts=[r['published_ns'] for r in owner if r.get('kind')=='harness_skill_first_publication'
                and r.get('request_id') in ids and intents[r['request_id']]['skill']=='approach_grasp']
        if not ends or not starts or max(ends)<min(starts):continue
        start,end=min(starts),max(ends)
        if any(r.get('kind')=='authority_revoked' and start<=r['monotonic_ns']<=end for r in owner):continue
        return dict(motion_s=(end-start)/1e9,verified_s=(reset['monotonic_ns']-start)/1e9,
                    reset_request_id=request,scope=scope['task_hash'])
    return None


def _transport_timing(owner, accepted):
    """Attribute solves, rechecks and first output to the same owner request."""
    requests = {}
    pending = {}
    confirmed = {}
    first_output = None
    first_after_grasp = None
    for row in owner:
        kind = row['kind']
        result = row.get('result') or {}
        request = row.get('request_id') or result.get('request_id') or (row.get('proof') or {}).get('request_id')
        intent = accepted.get(request, {}).get('intent') or row.get('intent', {})
        scope = tuple(intent.get(k) for k in ('task_hash', 'session_id', 'owner_instance_id', 'epoch'))
        if kind == 'skill_result' and intent.get('skill') in ('grasp', 'regrasp', 'release', 'release_supported'):
            confirmed.pop(scope, None)
        if (kind == 'skill_result' and intent.get('skill') == 'verify_grasp'
                and result.get('outcome') == 'success' and result.get('evidence_ids')):
            confirmed[scope] = row['monotonic_ns']
        if intent.get('skill') != 'transfer_above_hole':
            continue
        request = intent['request_id']
        item = requests.setdefault(request, dict(request_id=request, solve_count=0,
            solves=[], revalidation_count=0, revalidated_count=0, first_publication_ns=None))
        if kind == 'harness_planning_started':
            item['solve_count'] += 1
            pending[request] = row['monotonic_ns']
        elif kind == 'harness_plan_waiting_fresh_revalidation':
            started = pending.pop(request, None)
            elapsed = row['monotonic_ns'] - started if started is not None else None
            candidate = row.get('candidate') or {}
            stages = candidate.get('geometry_trace') or []
            evaluation = next((stage['candidate_evaluation'] for stage in stages
                if isinstance(stage, dict) and stage.get('candidate_evaluation')), {})
            item['solves'].append(dict(seconds=elapsed/1e9 if elapsed is not None and elapsed >= 0 else None,
                candidate_variant=candidate.get('candidate_variant'),
                attempts=[{k:v for k,v in attempt.items() if k in ('variant','elapsed_ns','outcome','route_family')}
                          for attempt in evaluation.get('attempts', ())]))
        elif kind == 'harness_revalidation_started':
            item['revalidation_count'] += 1
        elif kind == 'harness_plan_revalidated':
            item['revalidated_count'] += 1
        elif kind == 'harness_skill_first_publication':
            stamp = row['published_ns']
            item['first_publication_ns'] = stamp
            if first_output is None:
                first_output = request
                contact = confirmed.get(scope)
                if None not in scope and contact is not None and stamp >= contact:
                    first_after_grasp = (stamp - contact)/1e9
    return dict(requests=list(requests.values()), first_published_request=first_output,
                verified_grasp_to_first_transfer_publication_s=first_after_grasp)


def timing_report(run):
    root=Path(run)
    owner=_rows(root/'collected/owner/harness/trace.jsonl')
    planner_path=root/'planner/trace.jsonl'
    planner=_rows(planner_path)
    config=json.loads((root/'workspace-run.json').read_text()) if (root/'workspace-run.json').is_file() else {}
    accepted={r['intent']['request_id']:r for r in owner if r.get('kind')=='skill_accepted'}
    stops=[r['monotonic_ns'] for r in owner if r.get('kind')=='authority_revoked']
    steps=[]
    for request,entry in accepted.items():
        rows=[r for r in owner if r.get('request_id')==request or r.get('result',{}).get('request_id')==request]
        published=next((r['published_ns'] for r in rows if r['kind']=='harness_skill_first_publication'),None)
        result=next((r for r in rows if r['kind']=='skill_result'),None)
        waypoints=[r['measured_stamp_ns'] for r in rows if r['kind']=='harness_measured_waypoint']
        waits=[r for r in rows if r['kind']=='harness_verification_wait']
        after=min((r['after_ns'] for r in waits),default=None)
        end=result['monotonic_ns'] if result else next((t for t in stops if t>=entry['monotonic_ns']),None)
        def seconds(a,b):
            return (b-a)/1e9 if a is not None and b is not None and b>=a else None
        steps.append(dict(skill=entry['intent']['skill'],request_id=request,
            outcome=result['result']['outcome'] if result else 'no_terminal_skill_result',
            accepted_to_first_publication_s=seconds(entry['monotonic_ns'],published),
            first_publication_to_last_measured_waypoint_s=seconds(published,max(waypoints,default=None)),
            missing_evidence_wait_s=seconds(after,end),verification_wait_count=len(waits),
            accepted_to_terminal_or_revocation_s=seconds(entry['monotonic_ns'],end)))
    receipt=next((r for r in planner if r.get('kind')=='owner_event'
                  and r.get('record',{}).get('kind')=='harness_skill_first_publication'),None)
    startup=_elapsed(config.get('started_ns'), receipt.get('monotonic_ns')) if receipt else None
    models=[r for r in planner if r.get('kind')=='model_response']
    cycle=measured_cycle(owner,accepted)
    return dict(schema='f1-harness-timing-v1',run=str(root.resolve()),
        **_budget_report(owner, planner, config.get('started_ns'), planner_path.is_file()),
        owner_startup=owner_startup_report(root),
        click_to_first_publication_receipt_s=startup,
        model_response_s=(sum(r['elapsed_s'] for r in models)
                          if all(type(r.get('elapsed_s')) in (int, float) for r in models) else None),
        model_responses=len(models),
        model_transport=[{k:r[k] for k in ('kind','request_id','attempt','phase_s','last_phase',
            'connection_reused','origin_write_started','automatic_retry','code',
            'provider_request_id','provider_processing_ms') if k in r}
            for r in planner if r.get('kind') in ('model_transport_timing','model_transport_error')],
        model_connection_preparation=[{k:r[k] for k in ('elapsed_s','http_status','code','phase_s',
            'connection_reused','generation_requested') if k in r}
            for r in planner if r.get('kind')=='model_connection_prepared'],
        model_requests_without_receipt=max(0,sum(r.get('kind')=='model_request_started' for r in planner)-len(models)),
        transport=_transport_timing(owner,accepted),
        owner_steps=steps,full_grasp_place_reset_s=cycle['motion_s'] if cycle else None,
        full_cycle_verified_s=cycle['verified_s'] if cycle else None,
        full_cycle_status='owner_measured_complete' if cycle else 'not_established_without_measured_safe_reset',
        full_cycle_interval='first_approach_publication_to_measured_return_arrival',
        limitations=['Cross-host monotonic clocks are never subtracted.',
                     'Budget phases after first publication use successive owner milestones; missing boundaries are unknown.',
                     'Click intervals end at the coordinator receipt. Model time is charged to the phase visible at request start.',
                     'The placement target excludes reset. Budgets are retrospective and never control motion.',
                     'Owner startup covers only emitted process-local intervals, not spawn-to-ready or task success.',
                     'Receipt time is not physical motion onset.',
                     'Last measured waypoint may be intermediate; it does not prove skill completion.',
                     'Intervals can overlap; do not sum them into cycle time.'])

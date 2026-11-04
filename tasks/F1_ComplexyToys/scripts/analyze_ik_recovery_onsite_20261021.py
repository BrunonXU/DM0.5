"""Join actual powered recovery sessions to live server predictions; no robot I/O."""
import collections
import hashlib
import json
from pathlib import Path


def rows(path):
    out=[]
    for line in path.read_text().splitlines():
        try:out.append(json.loads(line))
        except json.JSONDecodeError:
            # A running journal snapshot may end with one incomplete record.
            if line != path.read_text().splitlines()[-1]:raise
    return out


def main():
    root=Path(__file__).resolve().parents[1]/'Eval/artifacts/20261021-ik-recovery-onsite'
    server_path=root/'server-logs/requests.jsonl'
    lookup={(s['session_id'],s['request_id'],s['policy_epoch']):s for s in rows(server_path)}
    report=[];sources=[Path(__file__),server_path]
    for folder in sorted(root.glob('f1_hil*')):
        journal=folder/'journal'
        complete=journal.exists()
        if not complete:journal=folder/'journal-snapshot'
        events_path=journal/'events.jsonl';events=rows(events_path);sources.append(events_path)
        start=next(e['monotonic_ns'] for e in events if e['event']=='local_policy_authorized')
        counts=collections.Counter(e['event'] for e in events)
        adopted={e['request_id'] for e in events if e['event']=='rtc_plan_adopted'}
        requests=[]
        for e in events:
            if e['event']!='policy_transport_timing':continue
            t=e['timing'];s=lookup[(t['session_id'],e['request_id'],e['policy_epoch'])]
            assert t['response_verified'] and s['model_id']=='f1-toys-eef-1d55a3e0017742b0'
            recovery=s.get('ik_recovery');rescued=bool(recovery and s['success'])
            if recovery:
                assert recovery['eef_postprocessed'] is False and recovery['ranges_changed'] is False
                assert recovery['attempts'][0]['success'] is False
            if rescued:assert e['request_id'] in adopted
            requests.append(dict(request_id=e['request_id'],policy_epoch=e['policy_epoch'],
                t_s=(e['monotonic_ns']-start)/1e9,success=s['success'],
                first_prediction_ik_rejected=bool(recovery),rescued_full=rescued,
                adopted=e['request_id'] in adopted,selected_order=recovery['selected_order'] if recovery else 0,
                client_total_ms=t['client_total_ms']))
        pauses=[dict(request_id=e['request_id'],reason=e['reason'],t_s=(e['monotonic_ns']-start)/1e9)
                for e in events if e['event']=='rtc_timing_paused']
        report.append(dict(episode=folder.name,journal_finalized=complete,requests=len(requests),
            configured=counts['rtc_prefix_recovery_configured']==1,
            first_prediction_ik_rejected=sum(r['first_prediction_ik_rejected'] for r in requests),
            rescued_full_adopted=sum(r['rescued_full'] for r in requests),
            final_rejected=sum(not r['success'] for r in requests),
            partial_started=counts['rtc_prefix_recovery_started'],partial_stopped=counts['rtc_prefix_recovery_stopped'],
            inference_deadline_pauses=sum(p['reason']=='inference_deadline' for p in pauses),
            prefix_stop_timeouts=sum(p['reason']=='prefix_recovery_stop_timeout' for p in pauses),
            operator_reported_success=folder.name.endswith(('4ffb474b','a79c99a1')),
            task_success_from_telemetry_alone=False,request_details=requests,pauses=pauses))
    result=dict(sessions=report,robot_commands=0,model_calls=0,online_changes=0,
        source_sha256={str(p.relative_to(root)) if p.is_relative_to(root) else str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
        limitations=['Two task successes are reported by the onsite user; this analysis does not infer task success from IK or gripper values.',
                     'The latest session journal was copied while paused/running and is explicitly a snapshot.',
                     'Rescue counts compare the first and selected candidate of the same live request, not a hypothetical full old-controller rollout.'])
    (root/'live-recovery-analysis.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps([{k:v for k,v in r.items() if k not in ('request_details','pauses')} for r in report],indent=2))


if __name__=='__main__':main()

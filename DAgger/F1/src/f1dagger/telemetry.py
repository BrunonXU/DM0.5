"""Post-session telemetry derived from journals, never part of motion decisions.

ROS publish success is not controller acceptance or physical arrival. Chunk
indices describe requested samples; they must not be labelled executed steps.
"""
import argparse
from collections import Counter
import json
from pathlib import Path


def summarize_session(run, *, termination=None, output=None):
    run = Path(run)
    journal = run / 'journal'
    if not journal.is_dir():
        journal = run / 'journal.partial'
    manifest_path = journal / 'manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    malformed = Counter()
    def rows(name):
        path = journal / (name + '.jsonl')
        if not path.exists():
            return
        with path.open() as stream:
            for line in stream:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    malformed[name] += 1
    events = Counter()
    requests, timings = set(), set()
    final_event = None
    for row in rows('events'):
        events[row['event']] += 1
        key = (row.get('policy_epoch'), row.get('request_id'))
        if row['event'] == 'powered_policy_request':
            requests.add(key)
        elif row['event'] == 'policy_transport_timing':
            timings.add(key)
        elif row['event'] == 'session_termination':
            final_event = row
    chunks = {}
    for row in rows('policy_chunks'):
        key = (row['policy_epoch'], row['request_id'])
        chunks[key] = dict(policy_epoch=key[0], request_id=key[1],
            planned_waypoints=len(row['actions']), observation_id=row['observation_id'],
            control_rows=0, published_control_rows=0, max_referenced_waypoint=None,
            last_sampling=None, first_control_ns=None, last_control_ns=None)
    max_error = [None] * 16
    control_rows = publications = 0
    sources = Counter()
    for row in rows('control'):
        control_rows += 1
        published = row.get('publication_complete') is True and row.get('submitted') is not None
        publications += int(published)
        sources[row.get('source', 'unknown')] += 1
        if published and row.get('measured') is not None:
            for i, (sent, measured) in enumerate(zip(row['submitted'], row['measured'])):
                error = sent - measured
                if max_error[i] is None or abs(error) > abs(max_error[i]['signed_error']):
                    max_error[i] = dict(signed_error=error, submitted=sent, measured=measured,
                                        monotonic_ns=row['monotonic_ns'])
        key = (row.get('policy_epoch'), row.get('policy_request_id'))
        chunk = chunks.get(key)
        if chunk is not None and row.get('source') == 'policy':
            chunk['control_rows'] += 1
            chunk['published_control_rows'] += int(published)
            chunk['first_control_ns'] = chunk['first_control_ns'] or row['monotonic_ns']
            chunk['last_control_ns'] = row['monotonic_ns']
            sample = row.get('policy_sampling') or {}
            chunk['last_sampling'] = sample
            index = sample.get('upper_index')
            if index is not None:
                previous = chunk['max_referenced_waypoint']
                chunk['max_referenced_waypoint'] = index if previous is None else max(previous, index)
    report = dict(schema='f1_session_telemetry_v1', episode_id=run.name,
        journal_complete=manifest.get('complete', False), binding=manifest.get('binding', {}),
        termination=termination or final_event or {'reason': 'not_recorded'},
        event_counts=dict(events), control_rows=control_rows, published_control_rows=publications,
        source_counts=dict(sources), max_submitted_feedback_error=max_error,
        error_units=['degree']*14+['gripper_0_to_100']*2,
        chunks=list(chunks.values()), missing_client_timing=[list(k) for k in sorted(requests-timings)],
        malformed_rows=dict(malformed), coverage=dict(controller_accepted_targets=False,
            controller_internal_commands=False, motor_current_torque=False,
            physical_waypoint_arrival=False, task_success='requires_scene_review',
            server_predictions='join server log by session_id/request_id/policy_epoch/model_version'),
        semantics='Published and sampled are distinct from physically executed; no inferred success label.')
    target = Path(output) if output else run/'telemetry-summary.json'
    with target.open('x') as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = summarize_session(args.run, output=args.output)
    print(json.dumps(dict(episode_id=result['episode_id'], control_rows=result['control_rows'],
                         missing_client_timing=result['missing_client_timing'])))

"""Read-only RTC-HIL evidence summary. Never approves human supervision."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

PROJECTS = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(PROJECTS/'DAgger/F1/src'), str(PROJECTS/'DAgger/shared/src')]
from f1dagger.recording import audit, read_rows


def summarize(raw):
    report = audit(raw)
    if not report['valid']:
        return report
    events = read_rows(raw/'journal/events.jsonl')
    control = read_rows(raw/'journal/control.jsonl')
    manifest = json.loads((raw/'journal/manifest.json').read_text())
    boundaries = [r for r in events if r['event'] in (
        'operator_start_authorized', 'rtc_hil_control_boundary', 'rtc_hil_takeover_anchor',
        'fresh_policy_to_policy', 'safe_hold_entered', 'session_termination',
        'home_completed_feedback', 'home_cancelled', 'home_stopped', 'rtc_hil_recovery_required')]
    origin = next((r['monotonic_ns'] for r in events if r['event']=='operator_start_authorized'),
                  events[0]['monotonic_ns'])
    for row in boundaries:
        row['seconds_from_start'] = (row['monotonic_ns']-origin)/1e9
    intervals = []
    for row in control:
        key = (row['source'], row['policy_epoch'], row['intervention_id'],
               row.get('submitted') is not None)
        if not intervals or intervals[-1]['key'] != key:
            intervals.append(dict(key=key, first_ns=row['monotonic_ns'], last_ns=row['monotonic_ns'], rows=0))
        intervals[-1]['last_ns'] = row['monotonic_ns']
        intervals[-1]['rows'] += 1
    for part in intervals:
        part.update(start_seconds=(part['first_ns']-origin)/1e9,
                    end_seconds=(part['last_ns']-origin)/1e9)
    joins = []
    for boundary in boundaries:
        if boundary['event'] != 'rtc_hil_takeover_anchor':
            continue
        rows = [r for r in control if r['policy_epoch']==boundary['policy_epoch'] and r.get('submitted')]
        first = rows[0] if rows else None
        joins.append(dict(epoch=boundary['policy_epoch'], first_published=first,
            preserves_previous_grippers=boundary['selected'][14:]==boundary['previous_submitted'][14:],
            preserves_previous_left=boundary['selected'][:7]==boundary['previous_submitted'][:7]))
    report.update(binding=manifest.get('binding'),
        event_counts=dict(Counter(r['event'] for r in events)), boundaries=boundaries,
        source_intervals=intervals, takeover_joins=joins,
        submitted_rows=sum(r.get('submitted') is not None for r in control),
        publications=sum(len(r.get('publications', [])) for r in control),
        publisher_gids=sorted({p['publisher_gid'] for r in control for p in r.get('publications', [])}),
        human_review_accepted=False, training_started=False)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('raw', type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.raw), ensure_ascii=False, indent=2))

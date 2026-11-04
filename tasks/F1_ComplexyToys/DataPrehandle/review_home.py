#!/usr/bin/env python3
"""Bind authorized Home supervision to recorded V1 feedback-completed intervals.

This is a technical eligibility review, not a fabricated visual success review.
Original journals and payloads remain unchanged. Raw audits run again at indexing.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

from build_training_index import sha256, HUMAN_SOURCES, HOME_ENDS


def review(raw, expected_digest):
    digest = sha256(raw / 'journal/manifest.json')
    if digest != expected_digest:
        raise ValueError('Raw identity changed: ' + raw.name)
    def rows(name):
        return [json.loads(line) for line in (raw / 'journal' / (name + '.jsonl')).open()]
    events, controls, observations = rows('events'), rows('control'), rows('observations')
    homes, active = [], None
    for event in events:
        if event['event'] == 'home_started':
            if active is not None:
                raise ValueError('Nested Home starts')
            active = event
        elif event['event'] in HOME_ENDS and active is not None:
            homes.append((active, event))
            active = None
    if active is not None:
        homes.append((active, None))
    decisions = []
    for start, end in homes:
        a, b = start['monotonic_ns'], end['monotonic_ns'] if end else controls[-1]['monotonic_ns']
        selected = [c for c in controls if a <= c['monotonic_ns'] <= b]
        commands = [c for c in selected if c['source'] == 'home']
        reason = None
        if start.get('preset') != '积木demo-V1':
            reason = 'different_home_preset_keep_separate'
        elif end is None or end['event'] != 'home_completed_feedback':
            reason = 'no_feedback_completed_event'
        elif not commands or any(c['publication_complete'] is not True or c['submitted'] is None
                                 or c['execution_mode'] != 'powered' for c in commands):
            reason = 'missing_or_invalid_published_home'
        elif any(c['source'] not in HUMAN_SOURCES | {'home'} for c in selected):
            reason = 'unexpected_control_source'
        goal = start.get('target', [])
        feedback = end.get('measured') if end else None
        decisions.append(dict(start_ns=a, end_ns=b, preset=start.get('preset'),
            completed_event=end['event'] if end else None, accepted=reason is None, exclusion_reason=reason,
            published_home_rows=len(commands), recorded_observations=sum(a <= o['available_monotonic_ns'] <= b for o in observations),
            target=goal, completion_measured=feedback,
            right_arm_completion_max_error_deg=max(abs(x-y) for x,y in zip(goal[7:14],feedback[7:14])) if feedback else None,
            max_left_command_change_deg=max((max(abs(c['submitted'][i]-commands[0]['submitted'][i]) for i in range(7)) for c in commands),default=0)))
    return dict(source_manifest_sha256=digest, decisions=decisions,
                accepted_intervals_ns=[[h['start_ns'],h['end_ns']] for h in decisions if h['accepted']])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--raw-root',type=Path,required=True)
    p.add_argument('--split',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    if a.output.exists(): raise FileExistsError(a.output)
    split=json.loads(a.split.read_text())
    result={r['episode']:review(a.raw_root/r['episode'],r['manifest_sha256']) for r in split['train']+split['validation']}
    decisions=[h for r in result.values() for h in r['decisions']]
    doc=dict(schema='f1_home_supervision_review_v1',created_utc=datetime.now(timezone.utc).isoformat(),
        authorization='User requested policy-learned return/Home and time-mask preprocessing on 2026-10-20; training waits for final recipe confirmation.',
        visual_success_reviewed=False, scope='Recorded V1 completed Home only; V0 and unconfirmed endings excluded, manual demonstration retained.',
        episodes=result,summary=dict(total_home_intervals=len(decisions),accepted=sum(h['accepted'] for h in decisions),
            exclusions=dict(Counter(h['exclusion_reason'] for h in decisions if not h['accepted'])),
            accepted_seconds=sum((h['end_ns']-h['start_ns'])/1e9 for h in decisions if h['accepted'])))
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(doc,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(doc['summary'],ensure_ascii=False))

if __name__=='__main__': main()

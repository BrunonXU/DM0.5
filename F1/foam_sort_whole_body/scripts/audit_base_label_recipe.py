#!/usr/bin/env python3
"""Compare the posthoc recipe, stored numeric labels and frozen visual review flags."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np
from audit_demo_signals import template_velocity

TASK = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=TASK/'results/base_anchor_review_20260915/recipe_audit')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    candidates = TASK/'results/demo_775_selection/candidates.jsonl'
    rows = [json.loads(line) for line in candidates.read_text().splitlines()]
    screen = TASK/'results/demo_775_phase_review/base_phase_screen'
    intervals = [json.loads(line) for line in (screen/'review_intervals.jsonl').read_text().splitlines()]
    flags = defaultdict(set)
    for interval in intervals:
        flags[interval['episode']].add(interval['priority'])
    groups = defaultdict(Counter)
    ratios, assumptions, records = Counter(), Counter(), []
    for row in rows:
        ep = row['episode']; path = Path(row['raw_path']).parent/'estimated_move_state.json'
        weak = json.loads(path.read_text()); windows = weak['detected_windows']; segments = weak['motion_segments']
        assert len(segments) == 6 and all(s[1] > s[0] for s in segments)
        outbound = windows['outbound_end_s'] - windows['outbound_start_s']
        returning = windows['return_end_s'] - windows['return_start_s']
        fractions = tuple(round((s[1]-s[0])/(outbound if i < 3 else returning), 8) for i,s in enumerate(segments))
        ratios[fractions] += 1
        assumptions[json.dumps(weak['assumptions'],sort_keys=True)] += 1
        assert sha(row['signal_npz_path']) == row['signal_npz_sha256']
        assert sha(row['camera_npz_path']) == row['camera_npz_sha256']
        with np.load(row['signal_npz_path'],allow_pickle=False) as signal, np.load(row['camera_npz_path'],allow_pickle=False) as camera:
            offset = (int(camera['head_log_ns'][0])-row['source_start_ns'])/1e9
            t = (signal['base_t_ns'] - row['source_start_ns'])/1e9
            expected = template_velocity(t,segments,offset)
            difference = float(np.max(abs(expected-signal['base_v'])))
            equal_times = bool(np.array_equal(signal['base_t_ns'], signal['hal_t_ns']))
            maxspeed = float(np.linalg.norm(signal['base_v'][:,:2],axis=1).max())
        method = windows['method']
        groups[method].update(sources=1, p1_sources=int(1 in flags[ep]),p2_sources=int(2 in flags[ep]),
                             p1_or_p2_sources=int(bool({1,2}&flags[ep])))
        records.append({'episode':ep,'method':method,'confidence':windows.get('confidence'),
                        'recipe_sha256':sha(path),'signal_sha256':row['signal_npz_sha256'],
                        'camera_sha256':row['camera_npz_sha256'],'first_head_offset_s':offset,
                        'json_windows':windows,'normalized_segment_durations':list(fractions),
                        'peak_speed_mps':maxspeed,'stored_base_times_equal_hal':equal_times,
                        'stored_base_matches_cubic_recipe_max_error_mps':difference,
                        'screen_priorities':sorted(flags[ep])})
    summary = {'created_at':datetime.now(timezone.utc).isoformat(),'sources':len(records),
               'candidate_sha256':sha(candidates),'screen_intervals_sha256':sha(screen/'review_intervals.jsonl'),
               'script_sha256':sha(__file__),'template_function_script_sha256':sha(Path(__file__).with_name('audit_demo_signals.py')),
               'method_counts_and_review_flags':{k:dict(v) for k,v in groups.items()},
               'normalized_segment_duration_patterns':[{'ratios':list(k),'sources':v} for k,v in ratios.items()],
               'geometry_assumption_patterns':[{'assumptions':json.loads(k),'sources':v} for k,v in assumptions.items()],
               'all_base_times_equal_hal':all(r['stored_base_times_equal_hal'] for r in records),
               'max_stored_recipe_velocity_error_mps':max(r['stored_base_matches_cubic_recipe_max_error_mps'] for r in records),
               'all_stored_velocity_matches_recipe_with_first_head_origin_within_1e_6':all(r['stored_base_matches_cubic_recipe_max_error_mps']<1e-6 for r in records),
               'fallback_is_only_hypothesis':False,
               'raw_label_generator_source_code_available':False,
               'interpretation':'Final labels numerically match the documented recipe and HAL log timestamps. Visual phase conflicts locate a recipe/observation discrepancy; without generator source the ORB/RANSAC/fallback implementation cause remains unproven. Review flags are not verified-error prevalence.',
               'labels_or_masks_changed':False}
    (args.output/'episodes.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records))
    (args.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()

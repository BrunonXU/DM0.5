#!/usr/bin/env python3
"""Read-only checks of supplied generator functions against frozen recipes.

Extract only pure math functions with AST; never import the bulk writer or run
either supplied main. Screenshot markers are tentative, not label overrides.
"""
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

TASK = Path(__file__).resolve().parents[1]
REFERENCE = TASK.parent / 'reference/20260915_base_label_scripts'
OUTPUT = TASK / 'results/base_script_audit_20260915'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    source = REFERENCE / 'bulk_write_move_state.py'
    manifest = json.loads((REFERENCE / 'manifest.json').read_text())
    for row in manifest['files']:
        assert sha(REFERENCE / row['file']) == row['sha256']
    tree = ast.parse(source.read_text())
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name in {'smooth', 'split_window', 'state_at'}]
    assert len(selected) == 3
    namespace = {'BACKWARD_M': .5, 'RIGHT_M': 1.6}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), namespace)
    candidates = TASK / 'results/demo_775_selection/candidates.jsonl'
    rows = [json.loads(line) for line in candidates.read_text().splitlines()]
    previous = TASK / 'results/base_anchor_review_20260915/recipe_audit/episodes.jsonl'
    audited = {r['episode']: r for r in map(json.loads, previous.read_text().splitlines())}
    records = []
    for row in rows:
        recipe_path = Path(row['raw_path']).parent / 'estimated_move_state.json'
        recipe = json.loads(recipe_path.read_text())
        assert sha(recipe_path) == audited[row['episode']]['recipe_sha256']
        w = recipe['detected_windows']
        segments = namespace['split_window'](w['outbound_start_s'], w['outbound_end_s'], True)
        segments += namespace['split_window'](w['return_start_s'], w['return_end_s'], False)
        offset = audited[row['episode']]['first_head_offset_s']
        duration = row['source_duration_ns'] / 1e9
        records.append({
            'episode': row['episode'], 'recipe_sha256': sha(recipe_path),
            'source_split_function_matches_stored_recipe_exactly':
                [list(s) for s in segments] == recipe['motion_segments'],
            'window_durations_s': [w['outbound_end_s'] - w['outbound_start_s'],
                                   w['return_end_s'] - w['return_start_s']],
            'all_segments_positive_duration': all(s[1] > s[0] for s in segments),
            'segments_ordered_nonoverlapping': all(b[0] >= a[1] for a, b in zip(segments, segments[1:])),
            'all_segments_within_source_recording': all(0 <= s[0] + offset < s[1] + offset <= duration for s in segments),
            'template_analytic_peak_speed_mps': max(1.5 * max(abs(s[4]-s[2]), abs(s[5]-s[3])) / (s[1]-s[0]) for s in segments),
        })
    target = 'all_data_3_derived/episode_000057'
    r = audited[target]
    windows = r['json_windows']
    offset = r['first_head_offset_s']
    old_out = [windows[k] + offset for k in ('outbound_start_s', 'outbound_end_s')]
    old_ret = [windows[k] + offset for k in ('return_start_s', 'return_end_s')]
    screenshot = {
        'episode': target, 'source': 'user screenshot in 2026-09-15 message',
        'status': 'tentative_user_draft_boundary_definition_unconfirmed',
        'time_axis': 'seconds since source MCAP start',
        'markers_s': {'grasp_complete': 5.030, 'outbound_start': 9.475, 'outbound_stop': 17.388,
                      'place_complete': 18.396, 'return_start': 18.783, 'return_stop': 26.431},
        'original_outbound_window_s': old_out, 'original_return_window_s': old_ret,
        'draft_minus_original_outbound_start_s': 9.475-old_out[0],
        'draft_minus_original_return_start_s': 18.783-old_ret[0],
        'old_return_method': windows['method'],
        'interpretation': 'If draft boundaries are confirmed, opposite-sign start errors rule out one global time shift. Outbound start must include initial backward motion; current screenshot could instead mark lateral start. No draft promoted to truth.',
        'used_to_generate_labels': False,
    }
    summary = {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'script_sha256': sha(Path(__file__)), 'supplied_source_sha256': sha(source),
        'reference_manifest_sha256': sha(REFERENCE/'manifest.json'),
        'candidate_sha256': sha(candidates), 'prior_recipe_audit_sha256': sha(previous),
        'sources': len(records),
        'exact_split_recipe_matches': sum(r['source_split_function_matches_stored_recipe_exactly'] for r in records),
        'nonpositive_segment_sources': [r['episode'] for r in records if not r['all_segments_positive_duration']],
        'overlapping_segment_sources': [r['episode'] for r in records if not r['segments_ordered_nonoverlapping']],
        'out_of_recording_segment_sources': [r['episode'] for r in records if not r['all_segments_within_source_recording']],
        'minimum_window_duration_s': min(min(r['window_durations_s']) for r in records),
        'sources_with_window_under_1_2_s': sum(min(r['window_durations_s']) < 1.2 for r in records),
        'peak_template_speed_range_mps': [min(r['template_analytic_peak_speed_mps'] for r in records), max(r['template_analytic_peak_speed_mps'] for r in records)],
        'collection_version_verified': False, 'original_scripts_main_executed': False,
        'labels_or_masks_changed': False, 'screenshot_comparison': screenshot,
    }
    OUTPUT.mkdir(exist_ok=True)
    for name, value in [('existing_recipe_check.json', summary), ('screenshot_000057_tentative.json', screenshot)]:
        path = OUTPUT / name
        if path.exists():
            raise FileExistsError(f'Preserve previous results: {path}')
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    (OUTPUT / 'existing_recipe_episodes.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in records))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

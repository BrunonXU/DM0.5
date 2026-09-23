#!/usr/bin/env python3
"""Independently reconcile the frozen pool and terminal-motion audit accounting."""
import hashlib
import json
from pathlib import Path
import numpy as np

TASK = Path(__file__).resolve().parents[1]
ROOT = TASK / 'results/demo_788_check'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(name):
    return json.loads((ROOT / name).read_text())


def main():
    selection = read('selection.json')
    assert digest(ROOT / 'candidates.jsonl') == selection['candidates_sha256']
    assert digest(ROOT / 'excluded.jsonl') == selection['excluded_sha256']
    assert digest(TASK / 'configs/demo_788_selection.json') == selection['config_sha256']
    candidates = {x['episode']: x for x in map(json.loads, (ROOT / 'candidates.jsonl').read_text().splitlines())}
    tails = {x['episode']: x for x in map(json.loads, (ROOT / 'signals/tail_reset_candidates.jsonl').read_text().splitlines())}
    assert len(candidates) == len(tails) == 788 and set(candidates) == set(tails)
    signals, follow, clocks, content = [read(name) for name in (
        'signals/summary.json', 'signals/followup_summary.json', 'clocks/summary.json', 'content/summary.json')]
    for record, key in ((signals, 'candidate_sha256'), (follow, 'candidate_sha256'), (clocks, 'candidate_manifest_sha256')):
        assert record[key] == selection['candidates_sha256']
    assert signals['numeric_integrity_pass_count'] == 788 and not signals['integrity_errors']
    assert clocks['audited_episodes'] == 788 and not clocks['errors']
    assert content['audited_sources'] == 788 and content['samples_expected'] == 7092
    assert read('content/verification.json')['success']
    for record, script in ((signals, 'audit_demo_signals.py'), (follow, 'audit_demo_signal_followup.py'),
                            (clocks, 'audit_demo_clocks.py')):
        assert digest(TASK / 'scripts' / script) == record['script_sha256']
    affected_chunks = affected_frames = inside = preceding = original_chunks = remaining_min = 0
    min_candidates = []
    for ep, row in candidates.items():
        p = Path(row['aligned_npz_path'])
        assert digest(p) == row['aligned_npz_sha256']
        tail = tails[ep]; a, b = tail['tail_start_frame_candidate'], tail['tail_stop_frame_exclusive']
        with np.load(p, allow_pickle=False) as arrays:
            n = len(arrays['time_ns']); assert 0 <= a < b <= n
            lead = arrays['action'][a:b, :14]; hal = arrays['state'][a:b, :14]
            assert np.isfinite(lead).all() and np.isfinite(hal).all()
            assert np.all(np.ptp(lead, axis=0) == 0)
            error = np.abs(hal - lead)
            assert np.any((np.ptp(hal, axis=0) > 5) & (error.max(axis=0) - error[0] > 5))
            starts = np.flatnonzero(arrays['chunk_start_candidate_valid'])
            touched = (starts < b) & (starts + 50 > a)
            within = (starts >= a) & (starts < b)
            before = (starts < a) & touched
            frames = int(arrays['joint_frame_candidate_valid'][a:b].sum())
            assert int(touched.sum()) == tail['existing_chunks_intersect_held_run']
            assert int(within.sum()) == tail['existing_chunk_starts_inside_held_run']
            assert int(before.sum()) == tail['existing_chunk_starts_before_held_run_crossing_boundary']
            assert frames == tail['existing_joint_valid_frames_inside_held_run']
            affected_chunks += int(touched.sum()); affected_frames += frames
            inside += int(within.sum()); preceding += int(before.sum()); original_chunks += len(starts)
            min_candidates.append(int((~touched).sum()))
    assert original_chunks == selection['totals']['chunk_starts'] == 892784
    assert affected_chunks == follow['existing_chunks_intersect_held_runs'] == 98620
    assert affected_frames == follow['existing_joint_valid_frames_inside_held_runs'] == 98613
    report = {'success': True, 'verified_sources': 788, 'candidate_sha256': selection['candidates_sha256'],
              'current_original_chunks': original_chunks, 'held_tail_intersecting_chunks': affected_chunks,
              'held_tail_joint_valid_frames': affected_frames, 'inside_tail_starts': inside,
              'before_tail_crossing_starts': preceding,
              'hypothetical_chunks_if_entire_held_tail_masked': original_chunks - affected_chunks,
              'hypothetical_min_remaining_chunks_per_source': min(min_candidates),
              'hypothetical_affected_chunk_fraction': affected_chunks / original_chunks,
              'tail_interval_status': 'diagnostic_conservative_range_not_confirmed_exact_reset_onset',
              'masks_changed': False, 'additional_source_exclusions_applied': False,
              'training_approved': False, 'raw_sha256_scope': 'none_full_collection; source_stat_and_derived_hashes_only'}
    report['report_hashes'] = {name: digest(ROOT / name) for name in (
        'selection.json', 'signals/summary.json', 'signals/followup_summary.json',
        'signals/tail_reset_candidates.jsonl', 'clocks/summary.json', 'content/summary.json',
        'content/verification.json', 'visual/base_phase_mismatch_evidence.json', 'visual/tail_visual_examples.json')}
    (ROOT / 'validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k:v for k,v in report.items() if k != 'report_hashes'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

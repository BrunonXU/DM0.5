#!/usr/bin/env python3
"""Join audits into a non-destructive review queue; does not approve training."""
import argparse
import collections
import csv
import json
from pathlib import Path
import yaml


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit-root', type=Path, required=True)
    parser.add_argument('--overrides', type=Path, default=Path(__file__).resolve().parents[1]/'configs/review_overrides.yaml')
    args = parser.parse_args()
    root = args.audit_root
    metadata = {r['episode']: r for r in read_jsonl(root/'metadata_audit/screening_manifest.jsonl')}
    cameras = {r['episode']: r for r in read_jsonl(root/'camera_timestamps/clock_review_manifest.jsonl')}
    messages = {str(Path(r['relative_path']).parent): r for r in read_jsonl(root/'mcap_audit/episodes.jsonl')}
    if set(metadata) != set(cameras) or set(metadata) != set(messages):
        raise ValueError('Audit episode sets differ; finish all audits on the same source first.')
    overrides = yaml.safe_load(args.overrides.read_text()).get('episodes', {}) if args.overrides.exists() else {}
    if set(overrides) - set(metadata):
        raise ValueError('Review overrides refer to episodes outside this audit.')
    output = root/'screening'
    output.mkdir(exist_ok=True)
    rows = []
    for episode in sorted(metadata):
        meta, camera, msg = metadata[episode], cameras[episode], messages[episode]
        clock_flags = sorted({r['flag'] for r in camera['flags']})
        control_gap = msg.get('timing', {}).get('/hal/joint_states', {}).get('log', {}).get('max_gap_s')
        peak = msg.get('move', {}).get('peak_linear_norm')
        reasons = []
        priority = 'P3_sample_then_annotate'
        if meta['manual_review'] or camera['requires_startup_frame_review']:
            priority = 'P2_startup_and_label_review'
            reasons.extend(meta['manual_review'])
            if camera['requires_startup_frame_review']:
                reasons.append('startup_frame_clock_and_freshness')
        if camera['requires_clock_mapping_review'] or 'offset_variation_review' in clock_flags or (peak is not None and peak > 2):
            priority = 'P1_clock_or_motion_review'
            if camera['requires_clock_mapping_review']:
                reasons.append('camera_clock_mapping')
            if 'offset_variation_review' in clock_flags:
                reasons.append('camera_offset_variation')
            if peak is not None and peak > 2:
                reasons.append('estimated_speed_over_2mps_review_not_hardware_limit')
        if not msg.get('audit_success') or meta['known_errors'] or (control_gap is not None and control_gap > 1) or 'late_first_sample_timestamp_outlier' in clock_flags:
            priority = 'P0_quarantine_affected_windows'
            if not msg.get('audit_success') or meta['known_errors']:
                reasons.append('file_or_message_integrity_issue')
            if control_gap is not None and control_gap > 1:
                reasons.append('feedback_gap_over_1s')
            if 'late_first_sample_timestamp_outlier' in clock_flags:
                reasons.append('late_first_camera_message_clock_outlier')
        rows.append({
            'episode': episode, 'priority': priority, 'reasons': sorted(set(reasons)),
            'camera_clock_flags': clock_flags, 'metadata_manual_flags': meta['manual_review'],
            'mcap_flags': msg.get('flags', []), 'max_feedback_gap_s': control_gap,
            'estimated_base_peak_linear_norm_mps': peak, 'duration_s': msg.get('duration_s'),
            'body_max_range_raw_units': msg.get('hal_group_max_range', {}).get('body'),
            'raw_retained': True, 'selected_for_training': False,
            'base_command_ground_truth_available': False, 'base_measured_supervision_loss_mask': 0,
            'waist_command_supervision_available': False, 'waist_action_loss_mask': 0,
            'arm_gripper_candidate': 'after_unit_time_contract_and_visual_outcome_review',
            'whole_cycle_success_annotation': None,
            'observed_transport_cycles': None, 'cycle_boundaries_finalized': False,
            'estimated_base_label_status': 'weak_label_pending_visual_validation',
            'review_override_evidence': None,
        })
        if episode in overrides:
            override = overrides[episode]
            row = rows[-1]
            row['priority'] = min(row['priority'], override['priority'])
            row['reasons'] = sorted(set(row['reasons']) | set(override.get('reasons', [])))
            for key in ['observed_transport_cycles', 'cycle_boundaries_finalized', 'estimated_base_label_status']:
                if key in override:
                    row[key] = override[key]
            row['review_override_evidence'] = override.get('evidence')
    (output/'review_manifest.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in rows))
    with (output/'review_queue.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in sorted(rows, key=lambda r: (r['priority'], r['episode'])):
            writer.writerow({k: ';'.join(v) if isinstance(v, list) else v for k,v in row.items()})
    summary = {
        'episodes': len(rows), 'priority_counts': dict(collections.Counter(r['priority'] for r in rows)),
        'selected_for_training': 0, 'raw_files_modified_or_deleted': 0,
        'review_overrides_applied': len(overrides),
        'meaning': 'Priorities guide review, not rejection of whole episodes or approval of training.',
        'P0': [r['episode'] for r in rows if r['priority'].startswith('P0')],
        'speed_threshold_note': '2 m/s is a review trigger; F1 validated limits are unknown.',
        'mask_note': 'Zero masks apply to treating estimated base as measured command and unavailable waist commands. Separate explicitly weak-label experiments remain possible.',
    }
    (output/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

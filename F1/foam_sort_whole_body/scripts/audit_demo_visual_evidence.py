#!/usr/bin/env python3
"""Quantify the data behind three explicitly identified visual spot checks."""
import hashlib
import json
from pathlib import Path
import numpy as np
from mcap.reader import make_reader
from mcap_ros2.decoder import DecoderFactory

TASK = Path(__file__).resolve().parents[1]
ROOT = TASK / 'results/demo_788_check'
OUT = ROOT / 'visual'


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    rows = {r['episode']: r for r in map(json.loads, (ROOT / 'candidates.jsonl').read_text().splitlines())}
    episode = 'all_data_2_derived/episode_000256'
    row = rows[episode]
    assert checksum(Path(row['signal_npz_path'])) == row['signal_npz_sha256']
    assert checksum(Path(row['aligned_npz_path'])) == row['aligned_npz_sha256']
    source = Path(row['raw_path'])
    source_before = source.stat()
    assert {'size_bytes': source_before.st_size, 'mtime_ns': source_before.st_mtime_ns} == row['source_fingerprint']
    hal_times, head_body = [], []
    columns = ['head_j1','head_j2','head_j3','body_j1','body_j2','body_j3','body_j4','body_j5']
    with source.open('rb') as stream:
        reader = make_reader(stream, decoder_factories=[DecoderFactory()], validate_crcs=True)
        for _, _, message, data in reader.iter_decoded_messages(topics=['/hal/joint_states']):
            values = dict(zip(data.name, data.position))
            hal_times.append((message.log_time - row['source_start_ns']) / 1e9)
            head_body.append([values[k] for k in columns])
    times = np.asarray(hal_times); values = np.asarray(head_body)
    diagnostics = []
    with np.load(row['signal_npz_path'], allow_pickle=False) as signals:
        base_times = (signals['base_t_ns'] - row['source_start_ns']) / 1e9
        velocity = signals['base_v']
        for begin, stop in ((7., 10.), (18., 20.)):
            selected = (base_times >= begin) & (base_times < stop)
            hal_selected = (times >= begin) & (times < stop)
            diagnostics.append({'interval_s_half_open': [begin, stop],
                                'base_samples': int(selected.sum()),
                                'base_all_zero': bool(np.all(velocity[selected] == 0)),
                                'hal_samples': int(hal_selected.sum()),
                                'head_body_columns': columns,
                                'head_body_peak_to_peak_raw_units': np.ptp(values[hal_selected], axis=0).tolist()})
    camera = json.loads((TASK / 'results/cleaning_v1/cameras' / (episode + '.json')).read_text())
    label_json = source.parent / 'estimated_move_state.json'
    labels = json.loads(label_json.read_text())
    record = {'episode': episode, 'candidate_manifest_sha256': checksum(ROOT / 'candidates.jsonl'),
              'source_fingerprint': row['source_fingerprint'], 'label_json_sha256': checksum(label_json),
              'json_windows_relative_to_first_head_image_s': labels['detected_windows'],
              'first_head_image_relative_to_source_s': (camera['cameras']['head']['first_log_ns'] - row['source_start_ns']) / 1e9,
              'head_steady_header_minus_log_s': camera['cameras']['head']['steady_median_header_minus_log_s'],
              'intervals': diagnostics,
              'visual_observation': 'Sparse head frames show the source basket giving way to the bag at 7–10s, and a return toward the basket at 18–20s; all recorded base velocity samples are zero in both intervals.',
              'interpretation': 'Strong evidence of incorrect weak base movement phases. Very small recorded head/body changes do not explain the large scene transition; exact corrected motion boundaries and true velocity remain unmeasured.',
              'disposition': 'must_exclude_or_relabel_before_using_as_joint_arm_base_supervision',
              'visual_review_scope': 'sparse_diagnostic_not_full_cycle_success_approval',
              'training_mask_modified': False, 'raw_modified': False}
    after = source.stat()
    assert (after.st_size, after.st_mtime_ns) == (source_before.st_size, source_before.st_mtime_ns)
    record['evidence_files'] = [{'file': p.name, 'sha256': checksum(p)} for p in sorted(OUT.glob('*.jpg'))]
    (OUT / 'base_phase_mismatch_evidence.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    tail_examples = []
    for ep, joint, begin, end in [('all_data_derived/episode_000192', 4, 54., 57.5),
                                  ('all_data_derived/episode_000093', 7, 59., 63.5)]:
        r = rows[ep]
        with np.load(r['aligned_npz_path'], allow_pickle=False) as data:
            t = (data['time_ns'] - r['source_start_ns']) / 1e9
            take = (t >= begin) & (t <= end)
            lead = data['action'][take, joint]; hal = data['state'][take, joint]
            tail_examples.append({'episode': ep, 'interval_s': [begin, end], 'zero_based_arm_joint_index': joint,
                                  'lead_min': float(lead.min()), 'lead_max': float(lead.max()),
                                  'hal_first': float(hal[0]), 'hal_last': float(hal[-1]),
                                  'interpretation': 'Images show arms returning toward the resting pose while the logged lead target stays at its old value; this supports a separate reset path but does not identify the acquisition software version.'})
    (OUT / 'tail_visual_examples.json').write_text(json.dumps(tail_examples, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'base_episode':episode,'zero_velocity_samples_by_interval':[x['base_samples'] for x in diagnostics], 'tail_visual_examples':len(tail_examples)},ensure_ascii=False))


if __name__ == '__main__':
    main()

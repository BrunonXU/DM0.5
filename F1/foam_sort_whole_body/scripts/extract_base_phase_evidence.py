#!/usr/bin/env python3
"""Re-read exact raw head JPEGs and feedback/weak-base messages for selected intervals."""
import argparse
import hashlib
import io
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from mcap.reader import make_reader
from mcap_ros2.decoder import DecoderFactory

TASK = Path(__file__).resolve().parents[1]
HEAD = '/camera/head/color/image_raw/compressed'
HAL = '/hal/joint_states'
BASE = '/move/State'
AXES = ['head_j1', 'head_j2', 'head_j3', *[f'body_j{i}' for i in range(1, 6)]]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--candidates', type=Path, default=TASK / 'results/demo_775_selection/candidates.jsonl')
    parser.add_argument('--cache', type=Path, default=TASK / 'results/demo_775_phase_review/frame_cache')
    args = parser.parse_args()
    rows = {r['episode']: r for r in map(json.loads, args.candidates.read_text().splitlines())}
    cases = json.loads(args.cases.read_text())
    args.output.mkdir(parents=True, exist_ok=False)
    summaries = []
    for case in cases:
        ep = case['episode']; row = rows[ep]; start = row['source_start_ns']
        cache_path = args.cache / 'sources' / (ep + '.npz')
        cache_meta = json.loads(cache_path.with_suffix('.json').read_text())
        assert sha(cache_path) == cache_meta['npz_sha256']
        with np.load(cache_path, allow_pickle=False) as pack:
            times = (pack['head_log_ns'] - start) / 1e9
            ids = np.flatnonzero((times >= case['start_s']) & (times <= case['stop_s']))
            assert len(ids) >= 3
            chosen = np.unique(ids[np.linspace(0, len(ids)-1, min(len(ids), 9)).round().astype(int)])
            wanted = {int(pack['head_log_ns'][i]): {'sample_index': int(i), 'jpeg_sha256': str(pack['head_jpeg_sha256'][i]), 'header_ns': int(pack['head_header_ns'][i])} for i in chosen}
        first, last = min(wanted), max(wanted)
        target = args.output / ep
        target.mkdir(parents=True)
        before = Path(row['raw_path']).stat()
        assert row['source_fingerprint'] == {'size_bytes': before.st_size, 'mtime_ns': before.st_mtime_ns}
        head_images, base, hal = [], [], []
        with Path(row['raw_path']).open('rb') as stream:
            reader = make_reader(stream, decoder_factories=[DecoderFactory()], validate_crcs=True)
            for _, channel, message, data in reader.iter_decoded_messages(topics=[HEAD, HAL, BASE], start_time=first - 100_000_000, end_time=last + 1):
                t = int(message.log_time)
                if channel.topic == HEAD and t in wanted:
                    jpeg = bytes(data.data)
                    assert hashlib.sha256(jpeg).hexdigest() == wanted[t]['jpeg_sha256']
                    header = int(data.header.stamp.sec)*10**9 + int(data.header.stamp.nanosec)
                    assert header == wanted[t]['header_ns']
                    name = f'head_{len(head_images):02d}_{(t-start)/1e9:.6f}s.jpg'
                    (target / name).write_bytes(jpeg)
                    head_images.append({'file': name, 'log_ns': t, 'source_s': (t-start)/1e9, 'header_ns': header, 'header_minus_log_s': (header-t)/1e9, 'sha256': wanted[t]['jpeg_sha256']})
                elif channel.topic == BASE:
                    base.append({'log_ns': t, 'value': [data.speed.linear.x, data.speed.linear.y, data.speed.angular.z]})
                elif channel.topic == HAL:
                    values = dict(zip(data.name, data.position))
                    hal.append({'log_ns': t, 'value': [values[x] for x in AXES]})
        assert len(head_images) == len(wanted)
        # Include the sample held at the first image, not only later messages.
        base_first = max(i for i, value in enumerate(base) if value['log_ns'] <= first)
        hal_first = max(i for i, value in enumerate(hal) if value['log_ns'] <= first)
        base, hal = base[base_first:], hal[hal_first:]
        bv = np.array([r['value'] for r in base]); ht = np.array([r['log_ns'] for r in hal]); hv = np.array([r['value'] for r in hal])
        assert np.isfinite(bv).all() and np.isfinite(hv).all()
        with np.load(row['signal_npz_path'], allow_pickle=False) as signal:
            for t, value in zip([r['log_ns'] for r in base], bv):
                i = int(np.searchsorted(signal['base_t_ns'], t))
                assert signal['base_t_ns'][i] == t and np.array_equal(signal['base_v'][i], value)
        after = Path(row['raw_path']).stat()
        assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
        sheet = Image.new('RGB', (1440, 300 * ((len(head_images)+2)//3)), '#15232c'); draw = ImageDraw.Draw(sheet)
        for j, record in enumerate(head_images):
            with Image.open(target / record['file']) as image:
                sheet.paste(image.resize((480, 270)), ((j%3)*480, (j//3)*300))
            draw.text(((j%3)*480+8, (j//3)*300+278), f'{record["source_s"]:.6f}s | base label {"ZERO" if np.all(bv == 0) else "see JSON"}', fill='white')
        sheet.save(target/'head_sheet.jpg', quality=92)
        record = {'episode': ep, 'case': case, 'raw_path': row['raw_path'], 'source_fingerprint': row['source_fingerprint'],
                  'candidate_sha256': sha(args.candidates), 'cache_npz_sha256': cache_meta['npz_sha256'], 'script_sha256': sha(__file__),
                  'first_image_s': (first-start)/1e9, 'last_image_s': (last-start)/1e9,
                  'raw_base_sample_count': len(base), 'raw_base_all_zero': bool(np.all(bv == 0)), 'raw_base_max_abs': np.max(abs(bv), axis=0).tolist(),
                  'raw_base_max_gap_s': float(np.max(np.diff([r['log_ns'] for r in base]))/1e9),
                  'raw_hal_sample_count': len(hal), 'raw_head_body_columns': AXES, 'raw_head_body_ptp_deg': np.ptp(hv, axis=0).tolist(),
                  'raw_hal_max_gap_s': float(np.max(np.diff(ht))/1e9), 'head_images': head_images,
                  'images_and_base_match_preexisting_caches': True, 'raw_source_unchanged': True,
                  'human_visual_review': 'pending', 'scope': 'sparse raw image/label/feedback interval, not full task outcome or metric velocity verification'}
        (target/'raw_samples.json').write_text(json.dumps({'base': base, 'hal': hal}, indent=2)+'\n')
        (target/'evidence.json').write_text(json.dumps(record, ensure_ascii=False, indent=2)+'\n')
        summaries.append(record)
    (args.output/'summary.json').write_text(json.dumps(summaries, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps([{'episode':r['episode'], 'base_all_zero':r['raw_base_all_zero'], 'interval_s':[r['first_image_s'],r['last_image_s']], 'head_body_ptp_deg':r['raw_head_body_ptp_deg']} for r in summaries],indent=2))


if __name__ == '__main__':
    main()

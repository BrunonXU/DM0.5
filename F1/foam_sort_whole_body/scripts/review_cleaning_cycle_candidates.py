#!/usr/bin/env python3
"""Create sparse visual evidence for the existing gripper-event review queue.

No automatic success/cycle labels are produced. Original MCAP is read only.
"""
import argparse
import io
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw
from mcap.reader import make_reader
from mcap_ros2.decoder import DecoderFactory

HEAD = '/camera/head/color/image_raw/compressed'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, default=Path('/mnt/dm05_nvme/dm05/datasets/f1_foam_sort/raw'))
    p.add_argument('--cleaning', type=Path, required=True)
    p.add_argument('--episode', action='append')
    args = p.parse_args()
    out = args.cleaning / 'cycle_review'
    out.mkdir(parents=True, exist_ok=True)
    rows = [json.loads(s) for s in (args.cleaning / 'episode_manifest.jsonl').read_text().splitlines()]
    rows = [r for r in rows if r['multiple_event_review_candidate'] and (not args.episode or r['episode'] in args.episode)]
    for row in rows:
        ep = row['episode']
        path = args.source / row['source_relative_path']
        st = path.stat()
        assert (st.st_size, st.st_mtime_ns) == (row['source_size_bytes'], row['source_mtime_ns'])
        duration = (row['end_ns'] - row['start_ns']) / 1e9
        targets = [0.5 + 3 * i for i in range(math.ceil((duration - 0.5) / 3))]
        targets += [max(0, duration - 0.5)]
        frames = []
        with path.open('rb') as f:
            reader = make_reader(f, decoder_factories=[DecoderFactory()])
            for t in sorted(set(targets)):
                ns = row['start_ns'] + round(t * 1e9)
                found = next(reader.iter_decoded_messages(topics=[HEAD], start_time=ns, end_time=min(row['end_ns']+1, ns+int(0.5e9))), None)
                tile = Image.new('RGB', (384, 244), '#17212b')
                draw = ImageDraw.Draw(tile)
                entry = {'requested_log_time_s': t}
                if found:
                    _, _, message, decoded = found
                    pic = Image.open(io.BytesIO(bytes(decoded.data))).convert('RGB')
                    pic.thumbnail((384, 216))
                    tile.paste(pic, ((384-pic.width)//2, 28+(216-pic.height)//2))
                    actual = (message.log_time-row['start_ns']) / 1e9
                    entry.update(log_time_ns=message.log_time, actual_log_time_s=actual,
                                 header_time_ns=decoded.header.stamp.sec*10**9+decoded.header.stamp.nanosec)
                    draw.text((4, 7), f'{actual:.3f}s (MCAP log)', fill='white')
                else:
                    draw.text((4, 7), f'{t:.3f}s NO FRAME in next 0.5s', fill='orange')
                    entry['missing'] = True
                frames.append((entry, tile))
        sheets = []
        for page, lo in enumerate(range(0, len(frames), 25)):
            group = frames[lo:lo+25]
            sheet = Image.new('RGB', (1920, 40+244*math.ceil(len(group)/5)), '#17212b')
            ImageDraw.Draw(sheet).text((8, 10), f'{ep} | sparse head frames | page {page+1} | not final cycle cuts', fill='white')
            for i, (_, tile) in enumerate(group):
                sheet.paste(tile, (384*(i%5), 40+244*(i//5)))
            name = ep.replace('/', '_')+f'_head_3s_{page+1}.jpg'
            sheet.save(out/name, quality=92)
            sheets.append(name)
        after = path.stat()
        assert (after.st_size, after.st_mtime_ns) == (st.st_size, st.st_mtime_ns)
        labels = json.loads((path.parent/'estimated_move_state.json').read_text())
        evidence = {'episode': ep, 'source_relative_path': row['source_relative_path'], 'duration_s': duration,
                    'start_ns': row['start_ns'], 'end_ns': row['end_ns'],
                    'source_fingerprint': {'size_bytes':st.st_size, 'mtime_ns':st.st_mtime_ns},
                    'source_unchanged': True, 'sampling': 'first head message at or after 3s-spaced log-time requests, within 0.5s',
                    'limitations': 'Sparse frames; no exposure calibration, no automatic success or cycle labels.',
                    'gripper_excursions': row['gripper_excursions'], 'estimated_move_state': labels,
                    'frames': [e for e, _ in frames], 'sheets': sheets}
        (out/(ep.replace('/', '_')+'_evidence.json')).write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n')
        print(f'{ep}: {len(frames)} samples, {len(sheets)} sheets', flush=True)


if __name__ == '__main__':
    main()

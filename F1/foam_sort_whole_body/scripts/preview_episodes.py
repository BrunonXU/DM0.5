#!/usr/bin/env python3
"""Read representative MCAP image messages and render phase contact sheets."""
import argparse
import io
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from mcap.reader import make_reader
from mcap_ros2.decoder import DecoderFactory

CAMERAS = {
    'head': '/camera/head/color/image_raw/compressed',
    'left': '/camera/left_wrist/color/image_raw/compressed',
    'right': '/camera/right_wrist/color/image_raw/compressed',
}
SAMPLES = [
    ('all_data_derived', 0), ('all_data_derived', 100), ('all_data_derived', 199),
    ('all_data_1_derived', 0), ('all_data_1_derived', 99),
    ('all_data_2_derived', 0), ('all_data_2_derived', 117), ('all_data_2_derived', 256),
    ('all_data_3_derived', 0), ('all_data_3_derived', 3),
    ('all_data_3_derived', 100), ('all_data_3_derived', 198), ('all_data_3_derived', 199),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('/mnt/dm05_nvme/dm05/datasets/f1_foam_sort/raw'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    audit = []
    head_rows = []
    for batch, number in SAMPLES:
        name = f'episode_{number:06d}'
        path = args.source / batch / name
        labels = json.loads((path / 'estimated_move_state.json').read_text())
        w = labels['detected_windows']
        with next(path.glob('*.mcap')).open('rb') as stream:
            reader = make_reader(stream, decoder_factories=[DecoderFactory()])
            stats = reader.get_summary().statistics
            start, end = stats.message_start_time, stats.message_end_time
            duration = (end - start) / 1e9
            times = [0.3, max(0.3, w['outbound_start_s'] - 0.5),
                     (w['outbound_start_s'] + w['outbound_end_s']) / 2,
                     (w['outbound_end_s'] + w['return_start_s']) / 2,
                     (w['return_start_s'] + w['return_end_s']) / 2, max(0, duration - 0.3)]
            phases = ['start', 'before outbound', 'outbound', 'destination', 'return', 'end']
            sheet = Image.new('RGB', (1920, 710), '#18212b')
            draw = ImageDraw.Draw(sheet)
            draw.text((8, 5), f'{batch}/{name} | estimated phase boundaries; not manual truth', fill='white')
            entries = []
            for col, (seconds, phase) in enumerate(zip(times, phases)):
                target = start + int(np.clip(seconds, 0, duration - 0.1) * 1e9)
                picked = {}
                for _, channel, message, decoded in reader.iter_decoded_messages(
                    topics=list(CAMERAS.values()), start_time=target, end_time=min(end + 1, target + int(0.5e9))
                ):
                    if channel.topic not in picked:
                        picked[channel.topic] = (message, decoded)
                    if len(picked) == len(CAMERAS):
                        break
                for row, (camera, topic) in enumerate(CAMERAS.items()):
                    x, y = col * 320, 28 + row * 226
                    draw.text((x + 5, y), f'{camera} | {phase} | {seconds:.2f}s', fill='white')
                    if topic not in picked:
                        draw.text((x + 5, y + 45), 'NO FRAME IN 0.5s WINDOW', fill='red')
                        entries.append({'camera': camera, 'phase': phase, 'missing': True})
                        continue
                    message, decoded = picked[topic]
                    image = Image.open(io.BytesIO(bytes(decoded.data)))
                    image.load()
                    stamp = decoded.header.stamp.sec * 10**9 + decoded.header.stamp.nanosec
                    entries.append({'camera': camera, 'phase': phase, 'width': image.width, 'height': image.height,
                                    'format': decoded.format, 'log_time_ns': message.log_time,
                                    'header_time_ns': stamp, 'log_minus_header_ms': (message.log_time - stamp) / 1e6})
                    image.thumbnail((318, 202))
                    sheet.paste(image, (x + (320-image.width)//2, y + 20))
            outfile = args.output / f'{batch}_{name}.jpg'
            sheet.save(outfile, quality=90)
            head_rows.append((batch + '/' + name, sheet.crop((0, 0, 1920, 254))))
            audit.append({'batch': batch, 'episode': name, 'sheet': outfile.name, 'frames': entries})
            print(f'Previewed {batch}/{name}: {len(entries)} frame checks', flush=True)
    overview = Image.new('RGB', (1920, 254 * len(head_rows)), '#18212b')
    for i, (_, row) in enumerate(head_rows):
        overview.paste(row, (0, i * 254))
    overview.save(args.output / 'head_overview.jpg', quality=90)
    (args.output / 'preview_audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2))
    body = '\n'.join(f'<h2>{x["batch"]}/{x["episode"]}</h2><img src="{x["sheet"]}" loading="lazy">' for x in audit)
    (args.output / 'index.html').write_text('<!doctype html><meta charset="utf-8"><title>F1 MCAP phase previews</title>'
                                          '<style>body{background:#18212b;color:white;font:16px sans-serif}img{max-width:100%}</style>'
                                          '<h1>F1 phase previews</h1><p>Phase boundaries come from estimated labels; visual sampling is not a success annotation.</p>' + body)


if __name__ == '__main__':
    main()

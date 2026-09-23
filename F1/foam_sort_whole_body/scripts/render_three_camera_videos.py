#!/usr/bin/env python3
"""Render complete MCAP episodes at 1x on the log-time axis, retaining gaps."""
import argparse
import html
import io
import json
import math
from pathlib import Path
import struct
import subprocess

from PIL import Image, ImageDraw, ImageFont
from mcap.reader import make_reader
from mcap_ros2.decoder import DecoderFactory

from audit_camera_timestamps import header_ns, schema_supported

CAMERAS = {
    'HEAD': '/camera/head/color/image_raw/compressed',
    'LEFT WRIST': '/camera/left_wrist/color/image_raw/compressed',
    'RIGHT WRIST': '/camera/right_wrist/color/image_raw/compressed',
}
SAMPLES = [
    ('all_data_derived/episode_000000', '第一批完整循环'),
    ('all_data_1_derived/episode_000000', '第二批；左腕 header 时钟偏移'),
    ('all_data_2_derived/episode_000117', '第三批；后补齐的 episode'),
    ('all_data_3_derived/episode_000198', '第四批完整循环'),
    ('all_data_2_derived/episode_000256', '补标速度峰值约 4.16 m/s 的条目'),
    ('all_data_2_derived/episode_000055', '身体反馈在约 32.18–34.78 秒断档'),
]


def image_payload(data):
    """Read standard little-endian ROS2 CompressedImage CDR (checked below)."""
    stamp = header_ns(data)
    pos = 12
    strings = []
    for _ in range(2):  # frame_id and format
        pos = 4 + ((pos - 4 + 3) // 4) * 4
        length, = struct.unpack_from('<I', data, pos)
        pos += 4
        if length < 1 or pos + length > len(data) or data[pos + length - 1] != 0:
            raise ValueError('Invalid CDR string')
        strings.append(data[pos:pos + length - 1].decode())
        pos += length
    pos = 4 + ((pos - 4 + 3) // 4) * 4
    length, = struct.unpack_from('<I', data, pos)
    pos += 4
    if pos + length != len(data):
        raise ValueError('Unexpected image sequence length')
    return stamp, strings[1], data[pos:pos + length]


def render(source, output, episode, reason, fps):
    path = next((source / episode).glob('*.mcap'))
    name = episode.replace('/', '_')
    target = output / (name + '.mp4')
    temporary = output / (name + '.partial.mp4')
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 18)
    small = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 15)
    size = (1920, 460)
    count = 0
    stats = {t: {'messages_read': 0, 'jpeg_decoded': 0, 'missing_output_frames': 0,
                 'held_over_100ms_output_frames': 0, 'decoder_crosscheck': False}
             for t in CAMERAS.values()}
    with path.open('rb') as stream, (output / (name + '.ffmpeg.log')).open('w') as log:
        reader = make_reader(stream, validate_crcs=True)
        summary = reader.get_summary()
        start, end = summary.statistics.message_start_time, summary.statistics.message_end_time
        duration = (end - start) / 1e9
        command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'rawvideo',
                   '-pix_fmt', 'rgb24', '-s', f'{size[0]}x{size[1]}', '-r', str(fps),
                   '-i', 'pipe:0', '-an', '-c:v', 'libx264', '-threads', '4',
                   '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p',
                   '-movflags', '+faststart', str(temporary)]
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=log)
        messages = iter(reader.iter_messages(topics=list(CAMERAS.values()), log_time_order=True))
        pending = next(messages, None)
        latest, cached = {}, {}
        try:
            for i in range(math.ceil(duration * fps)):
                now = start + round(i * 1e9 / fps)
                while pending is not None and pending[2].log_time <= now:
                    schema, channel, msg = pending
                    if not stats[channel.topic]['decoder_crosscheck']:
                        if not schema_supported(schema, channel):
                            raise ValueError('Unexpected image schema')
                        stamp, fmt, payload = image_payload(msg.data)
                        decoded = DecoderFactory().decoder_for(channel.message_encoding, schema)(msg.data)
                        assert stamp == decoded.header.stamp.sec * 10**9 + decoded.header.stamp.nanosec
                        assert fmt == decoded.format and payload == bytes(decoded.data)
                        stats[channel.topic]['decoder_crosscheck'] = True
                    stats[channel.topic]['messages_read'] += 1
                    latest[channel.topic] = msg
                    pending = next(messages, None)
                frame = Image.new('RGB', size, '#111820')
                draw = ImageDraw.Draw(frame)
                draw.text((12, 5), f'{episode}     t={i/fps:.2f}s / {duration:.2f}s     1x playback', font=font, fill='white')
                for col, (label, topic) in enumerate(CAMERAS.items()):
                    x = col * 640
                    draw.text((x + 8, 36), label, font=font, fill='#79caff')
                    msg = latest.get(topic)
                    if msg is None:
                        stats[topic]['missing_output_frames'] += 1
                        draw.text((x + 8, 190), 'NO FRAME YET', font=font, fill='#ffbe64')
                        continue
                    if topic not in cached or cached[topic][0] != msg.log_time:
                        stamp, fmt, payload = image_payload(msg.data)
                        with Image.open(io.BytesIO(payload)) as raw:
                            picture = raw.convert('RGB')
                            picture.thumbnail((638, 360), Image.Resampling.BILINEAR)
                        cached[topic] = (msg.log_time, stamp, picture)
                        stats[topic]['jpeg_decoded'] += 1
                    log_time, stamp, picture = cached[topic]
                    age = (now - log_time) / 1e6
                    frame.paste(picture, (x + (640-picture.width)//2, 64 + (360-picture.height)//2))
                    color = '#ffbe64' if age > 100 else 'white'
                    draw.text((x + 8, 426), f'log age {age:.0f}ms | header-log {(stamp-log_time)/1e9:+.3f}s', font=small, fill=color)
                    stats[topic]['held_over_100ms_output_frames'] += int(age > 100)
                proc.stdin.write(frame.tobytes())
                count += 1
                if i == min(round(10 * fps), math.ceil(duration * fps) - 1):
                    frame.save(output / (name + '.jpg'), quality=90)
            proc.stdin.close()
            if proc.wait() != 0:
                raise RuntimeError(f'ffmpeg failed; see {name}.ffmpeg.log')
        except BaseException:
            proc.kill()
            proc.wait()
            raise
    temporary.replace(target)
    probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
        'stream=codec_name,width,height,nb_frames:format=duration,size', '-of', 'json', str(target)]))
    assert int(probe['streams'][0]['nb_frames']) == count
    assert abs(float(probe['format']['duration']) - duration) <= 1/fps + .02
    result = dict(episode=episode, reason=reason, file=target.name, poster=name+'.jpg',
                  source=str(path), source_duration_s=duration, output_frames=count, fps=fps,
                  timeline='mcap_log_time_last_frame_at_or_before_t', playback_speed=1,
                  no_training_alignment_claim=True, camera_statistics=stats, ffprobe=probe)
    (output / (name + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f'Completed {episode}: {duration:.2f}s, {count} frames, {target.stat().st_size/1e6:.1f} MB', flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('/mnt/dm05_nvme/dm05/datasets/f1_foam_sort/raw'))
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1]/'results/20260914/videos')
    parser.add_argument('--fps', type=int, default=15)
    parser.add_argument('--episode', action='append', help='batch/episode_XXXXXX; default six representative episodes')
    args = parser.parse_args()
    if args.fps <= 0:
        parser.error('--fps must be positive')
    args.output.mkdir(parents=True, exist_ok=True)
    samples = [(x, '指定条目') for x in args.episode] if args.episode else SAMPLES
    results = [render(args.source, args.output, e, reason, args.fps) for e, reason in samples]
    (args.output/'manifest.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
    cards = '\n'.join(f'<article><h2>{html.escape(r["episode"])}</h2><p>{html.escape(r["reason"])} · {r["source_duration_s"]:.1f} 秒</p>'
        f'<video controls preload="metadata" poster="{r["poster"]}" src="{r["file"]}"></video>'
        f'<p><a href="{r["file"]}" download>下载 MP4</a></p></article>' for r in results)
    (args.output/'index.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>F1 三路视频</title>'
        '<style>body{margin:24px;background:#111820;color:#e9f0f5;font:16px system-ui}article{margin:32px 0}video{width:100%;max-width:1920px}a{color:#79caff}</style>'
        '<h1>F1 泡沫分拣 · 原始三路相机</h1><p>左：头部；中：左腕；右：右腕。完整录制区间，1 倍速，15 fps 预览，无音频。</p>'
        '<p>按 MCAP log 时间取不晚于当前时刻的最近帧。缺帧显示 NO FRAME YET；旧帧继续显示并标注 log age。'
        'header-log 为原始时钟差。本页用于查看原始记录，不代表相机曝光同步已经校正，未裁掉异常开头。</p>'+cards+'</html>')


if __name__ == '__main__':
    main()

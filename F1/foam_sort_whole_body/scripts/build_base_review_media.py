#!/usr/bin/env python3
"""Build six complete, causal three-camera base-review videos and provenance.

Read-only original MCAPs. This is a visual-review product, not corrected actions,
exposure synchronization, or a training/replay approval. Per-source completion
records allow resuming after a terminal/worker interruption.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import subprocess
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from mcap.reader import make_reader
from mcap_ros2.decoder import DecoderFactory

from audit_camera_timestamps import schema_supported
from render_three_camera_videos import CAMERAS, image_payload

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = [
    ('all_data_1_derived/episode_000009', '原图确认阶段冲突；当前已排除'),
    ('all_data_2_derived/episode_000000', '原图确认阶段冲突；当前已排除'),
    ('all_data_3_derived/episode_000057', '零标签与背景运动疑点，待人工复核'),
    ('all_data_derived/episode_000135', '较长零标签与背景运动疑点，待人工复核'),
    ('all_data_1_derived/episode_000096', '非零标签与低背景位移疑点，待人工复核'),
    ('all_data_2_derived/episode_000003', '未命中两类重点疑点；尚未验收'),
]
CAMERA_KEYS = ('head', 'left', 'right')
DISPLAY = {
    SAMPLES[0][0]: ('已确认 · 第一批 000009', 'confirmed_zero_motion', (11.468,17.475)),
    SAMPLES[1][0]: ('已确认 · 第二批 000000', 'confirmed_zero_motion', (10.983,14.485)),
    SAMPLES[2][0]: ('零速疑点 · 第三批 000057', 'zero_motion_review', (9.492,15.499)),
    SAMPLES[3][0]: ('长区间疑点 · 原批 000135', 'zero_motion_review', (36.978,57.470)),
    SAMPLES[4][0]: ('有速度、低位移 · 第一批 000096', 'nonzero_low_motion_review', (4.966,6.969)),
    SAMPLES[5][0]: ('未命中重点疑点 · 第二批 000003', 'no_priority_review', None),
}


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def save(path: Path, value, *, compact=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.writing')
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False,
                                   indent=None if compact else 2,
                                   separators=(',', ':') if compact else None) + '\n')
    temporary.replace(path)


def rows(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def finite(value):
    if isinstance(value, np.ndarray):
        return finite(value.tolist())
    if isinstance(value, list):
        return [finite(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def fingerprint(path):
    stat = path.stat()
    return {'size_bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def numeric_series(candidate, source_start, output, slug, fps, frame_count):
    """Retain original native sample times; frontend may sample causally."""
    path = Path(candidate['signal_npz_path'])
    assert sha(path) == candidate['signal_npz_sha256']
    series, numeric, numeric_valid = {}, {}, {}
    grid = source_start + np.rint(np.arange(frame_count,dtype=np.float64)*1e9/fps).astype(np.int64)
    def causal(ts, values):
        source_indices = np.searchsorted(ts,grid,side='right')-1
        idx = np.maximum(source_indices,0)
        copied = np.asarray(values[idx],dtype=np.float64).copy()
        if copied.ndim == 1:
            copied = copied[:,None]
        valid = ((source_indices >= 0) & ((grid-ts[idx]) <= 100_000_000)
                 & np.all(np.isfinite(copied),axis=1))
        copied[~valid] = np.nan
        return copied, valid
    with np.load(path, allow_pickle=False) as z:
        for name, time_key, data_key, unit in [
            ('base', 'base_t_ns', 'base_v', ['m/s', 'm/s', 'rad/s']),
            ('lead', 'lead_t_ns', 'lead_q', 'deg'),
            ('hal', 'hal_t_ns', 'hal_q', 'deg'),
            ('left_gripper_command', 'left_cmd_t_ns', 'left_cmd', '0=open,100=closed'),
            ('right_gripper_command', 'right_cmd_t_ns', 'right_cmd', '0=open,100=closed'),
            ('left_gripper_feedback', 'left_state_t_ns', 'left_state', '0=open,100=closed'),
            ('right_gripper_feedback', 'right_state_t_ns', 'right_state', '0=open,100=closed'),
        ]:
            assert np.all(np.diff(z[time_key]) >= 0)
            series[name] = {'t_s': ((z[time_key] - source_start) / 1e9).tolist(),
                            'values': finite(z[data_key]), 'unit': unit,
                            'resampled': False}
        for name, time_key, data_key in [('base','base_t_ns','base_v'),
                                       ('lead','lead_t_ns','lead_q'),('hal','hal_t_ns','hal_q')]:
            values = z[data_key][:,:14] if name == 'hal' else z[data_key]
            copied, valid = causal(z[time_key],values)
            numeric[name], numeric_valid[name] = finite(copied),valid.tolist()
        for field,suffix in [('gripper_command','cmd'),('gripper_feedback','state')]:
            left,left_valid = causal(z[f'left_{suffix}_t_ns'],z[f'left_{suffix}'])
            right,right_valid = causal(z[f'right_{suffix}_t_ns'],z[f'right_{suffix}'])
            numeric[field] = finite(np.column_stack((left[:,0],right[:,0])))
            numeric_valid[field] = (left_valid & right_valid).tolist()
    phase_path = ROOT/'results/demo_775_phase_review/base_phase_screen/sources'/Path(candidate['episode']).with_suffix('.json')
    phase = json.loads(phase_path.read_text())
    pair_path = phase_path.with_suffix('.pairs.jsonl')
    assert sha(pair_path) == phase['pairs_sha256']
    pair_data = []
    for pair in rows(pair_path):
        pair_data.append({key: pair[key] for key in ('pair_index', 'start_s', 'end_s', 'evidence_status', 'uncertainty_reasons')} | {
            **{key: pair['motion'].get(key) for key in ('status', 'speed_px_s', 'confidence', 'dx_px', 'dy_px')},
            'head_max_ptp_deg':pair['head_body'].get('head_max_ptp_deg'),
            'body_max_ptp_deg':pair['head_body'].get('body_max_ptp_deg'),
        })
    original_json = Path(candidate['raw_path']).parent/'estimated_move_state.json'
    template = json.loads(original_json.read_text())
    camera_path = Path(candidate['camera_npz_path'])
    with np.load(camera_path, allow_pickle=False) as z:
        origin_offset = (int(z['head_log_ns'][0]) - source_start) / 1e9
    native_data = {
        'schema': 'f1_base_review_native_signals_v1', 'episode': candidate['episode'],
        'source_start_ns': str(source_start), 'timeline': 'seconds_from_mcap_log_origin',
        'series': series, 'background_pairs': pair_data, 'suspect_intervals': phase['intervals'],
        'tail_mask_start_s': phase['tail_boundary']['relative_source_s'],
        'tail_mask_meaning': 'existing_all_19D_mask_start_not_physical_stop_or_reset_onset',
        'weak_template_original': template, 'template_origin_head_log_offset_s': origin_offset,
        'template_segments_mcap_s': [
            {'start_s': segment[0]+origin_offset, 'end_s': segment[1]+origin_offset,
             'start_xy_m': segment[2:4], 'end_xy_m': segment[4:6], 'name': segment[6]}
            for segment in template.get('motion_segments', [])
        ],
        'template_windows_mcap_s': {
            key: (value+origin_offset if key.endswith('_s') and isinstance(value, (float,int)) else value)
            for key,value in template.get('detected_windows', {}).items()
        },
        'base_provenance': 'posthoc_estimated_weak_label_not_measured_odometry',
        'signal_sampling_rule': 'native_sample_times_no_action_shift_no_HAL_reconstruction',
        'signal_npz_sha256': candidate['signal_npz_sha256'],
        'phase_screen_sha256': sha(phase_path), 'background_pairs_sha256': sha(pair_path),
        'estimated_move_state_sha256': sha(original_json),
        'source_accepted': False, 'labels_reconstructed': False,
    }
    native_target = output/'data'/f'{slug}.native_signals.json'
    save(native_target,native_data,compact=True)
    template_windows = native_data['template_windows_mcap_s']
    label_windows = [{'label': label,'start_s':template_windows[f'{key}_start_s'],
                      'end_s':template_windows[f'{key}_end_s']}
                     for label,key in [('原去程','outbound'),('原返程','return')]]
    data = {'episode':candidate['episode'],'source_origin_ns':str(source_start),
        'time_s':(np.arange(frame_count,dtype=np.float64)/fps).tolist(),
        'numeric':numeric,'numeric_valid':numeric_valid,'motion_pairs':pair_data,
        'review_intervals':[{key:interval[key] for key in ('start_s','end_s','kind','priority')}
                            for interval in phase['intervals']],
        'label_windows':label_windows,'gripper_events':[],
        'label_method':template.get('detected_windows',{}).get('method'),
        'label_confidence':template.get('detected_windows',{}).get('confidence'),
        'label_segments':[{'start_s':s[0]+origin_offset,'end_s':s[1]+origin_offset,
            'axis':'x' if s[4] != s[2] and s[5] == s[3] else ('y' if s[4] == s[2] and s[5] != s[3] else 'xy'),
            'distance_m':math.hypot(s[4]-s[2],s[5]-s[3]),
            'delta_x_m':s[4]-s[2],'delta_y_m':s[5]-s[3],'label':s[6]}
            for s in template.get('motion_segments',[])],
        'tail_mask_start_s':phase['tail_boundary']['relative_source_s'],
        'provenance':{'schema':'f1_base_review_ui_data_v1','native_signals_file':native_target.name,
            'native_signals_sha256':sha(native_target),'signal_npz_sha256':candidate['signal_npz_sha256'],
            'camera_npz_sha256':candidate['camera_npz_sha256'],'phase_screen_sha256':sha(phase_path),
            'label_json_sha256':sha(original_json),'first_head_offset_s':origin_offset,
            'numeric_resampling':'latest_source_at_or_before_query; no prior or age>0.1s -> null',
            'camera_resampling':'latest_raw_image_at_or_before_query; missing/stale shown visibly',
            'preview_applies_training_timestamp_mask':False,'exposure_alignment_claim':False,
            'HAL_reconstructed':False,'base_labels_reconstructed':False,
            'template_origin_head_log_offset_s':origin_offset,
            'template_offset_applied_once_to_JSON_windows_only':True,
            'raw_base_signal_shifted':False,'source_accepted':False}}
    target=output/'data'/f'{slug}.json'
    save(target,data,compact=True)
    return target,native_target


def render(candidate, reason, current_sources, args, identity):
    episode = candidate['episode']
    slug = episode.replace('/', '_')
    output = args.output
    evidence_path = output/'data'/f'{slug}.evidence.json'
    raw_path = Path(candidate['raw_path'])
    assert fingerprint(raw_path) == candidate['source_fingerprint']
    if evidence_path.exists():
        old = json.loads(evidence_path.read_text())
        if old.get('identity') != identity:
            raise RuntimeError(f'Refusing incompatible previous product: {evidence_path}')
        if old.get('complete'):
            for relative, digest in old['output_sha256'].items():
                assert sha(output/relative) == digest, relative
            print(f'Cached {episode}', flush=True)
            return old['manifest_entry']

    started = time.monotonic()
    video = output/'media'/f'{slug}.mp4'
    temporary = output/'media'/f'{slug}.partial.mp4'
    poster = output/'media'/f'{slug}.jpg'
    provenance = output/'data'/f'{slug}.frames.json'
    camera_path = Path(candidate['camera_npz_path'])
    assert sha(camera_path) == candidate['camera_npz_sha256']
    with np.load(camera_path, allow_pickle=False) as z:
        camera_index = {key: {'logs': z[f'{key}_log_ns'].copy(),
                              'headers': z[f'{key}_header_ns'].copy()}
                        for key in CAMERA_KEYS}
    start = int(candidate['source_start_ns'])
    duration = int(candidate['source_duration_ns'])/1e9
    frame_count = math.ceil(duration*args.fps)
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 18)
    small = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 15)
    size = (1920, 460)
    stats = {key: {'messages_read': 0, 'jpeg_decoded': 0, 'missing_output_frames': 0,
                   'held_over_100ms_output_frames': 0, 'decoder_crosscheck': False}
             for key in CAMERA_KEYS}
    frame_rows, selected = [], {key: {} for key in CAMERA_KEYS}
    topic_keys = dict(zip(CAMERAS.values(), CAMERA_KEYS))
    latest, cached = {}, {}

    def consume(pending):
        schema, channel, msg = pending
        key = topic_keys[channel.topic]
        index = stats[key]['messages_read']
        assert index < len(camera_index[key]['logs'])
        assert int(camera_index[key]['logs'][index]) == msg.log_time
        if not stats[key]['decoder_crosscheck']:
            assert schema_supported(schema, channel)
            stamp, fmt, payload = image_payload(msg.data)
            decoded = DecoderFactory().decoder_for(channel.message_encoding, schema)(msg.data)
            assert stamp == decoded.header.stamp.sec*10**9 + decoded.header.stamp.nanosec
            assert fmt == decoded.format and payload == bytes(decoded.data)
            stats[key]['decoder_crosscheck'] = True
        stats[key]['messages_read'] += 1
        latest[key] = (index, msg)

    with raw_path.open('rb') as stream, (output/'media'/f'{slug}.ffmpeg.log').open('w') as log:
        reader = make_reader(stream, validate_crcs=True)
        summary = reader.get_summary()
        assert summary.statistics.message_start_time == start
        assert abs(summary.statistics.message_end_time - (start+int(candidate['source_duration_ns']))) <= 1
        proc = subprocess.Popen([
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'rawvideo',
            '-pix_fmt', 'rgb24', '-s', '1920x460', '-r', str(args.fps), '-i', 'pipe:0',
            '-an', '-c:v', 'libx264', '-threads', '4', '-preset', 'veryfast', '-crf', '23',
            '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(temporary)],
            stdin=subprocess.PIPE, stderr=log)
        messages = iter(reader.iter_messages(topics=list(CAMERAS.values()), log_time_order=True))
        pending = next(messages, None)
        try:
            for i in range(frame_count):
                query = start + round(i*1e9/args.fps)
                while pending is not None and pending[2].log_time <= query:
                    consume(pending)
                    pending = next(messages, None)
                canvas = Image.new('RGB', size, '#111820')
                draw = ImageDraw.Draw(canvas)
                draw.text((12,5), f'{episode}   t={i/args.fps:.2f}s / {duration:.2f}s   1x playback', font=font, fill='white')
                frame_meta = {'frame_index': i, 't_s': i/args.fps, 'query_log_ns': str(query), 'cameras': []}
                for col, ((label, _topic), key) in enumerate(zip(CAMERAS.items(), CAMERA_KEYS)):
                    x = 640*col
                    draw.text((x+8,36), label, font=font, fill='#79caff')
                    camera_meta = {'key': key, 'source_index': None, 'log_ns': None,
                                   'header_ns': None, 'age_s': None, 'missing': True}
                    if key not in latest:
                        stats[key]['missing_output_frames'] += 1
                        draw.text((x+8,190), 'NO FRAME YET', font=font, fill='#ffbe64')
                    else:
                        index, msg = latest[key]
                        if key not in cached or cached[key][0] != index:
                            stamp, fmt, payload = image_payload(msg.data)
                            assert stamp == int(camera_index[key]['headers'][index])
                            with Image.open(io.BytesIO(payload)) as raw:
                                raw.load()
                                picture = raw.convert('RGB')
                                picture.thumbnail((638,360), Image.Resampling.BILINEAR)
                            cached[key] = (index, stamp, picture)
                            stats[key]['jpeg_decoded'] += 1
                            selected[key][index] = {'source_index': index, 'log_ns': str(msg.log_time),
                                'header_ns': str(stamp), 'jpeg_sha256': hashlib.sha256(payload).hexdigest()}
                        _index, stamp, picture = cached[key]
                        age = (query-msg.log_time)/1e9
                        assert age >= 0
                        canvas.paste(picture, (x+(640-picture.width)//2,64+(360-picture.height)//2))
                        draw.text((x+8,426), f'log age {age*1000:.0f}ms | header-log {(stamp-msg.log_time)/1e9:+.3f}s',
                                  font=small, fill='#ffbe64' if age>.1 else 'white')
                        stats[key]['held_over_100ms_output_frames'] += int(age>.1)
                        camera_meta.update(source_index=index, log_ns=str(msg.log_time), header_ns=str(stamp),
                                           age_s=age, missing=False, stale_over_100ms=age>.1)
                    frame_meta['cameras'].append(camera_meta)
                frame_rows.append(frame_meta)
                proc.stdin.write(canvas.tobytes())
                if i == min(round(8*args.fps),frame_count-1):
                    canvas.save(poster, quality=90)
            while pending is not None:
                consume(pending)
                pending = next(messages, None)
            proc.stdin.close()
            if proc.wait() != 0:
                raise RuntimeError(f'ffmpeg failed for {episode}')
        except BaseException:
            proc.kill()
            proc.wait()
            raise
    for key in CAMERA_KEYS:
        assert stats[key]['messages_read'] == len(camera_index[key]['logs'])
    temporary.replace(video)
    probe = json.loads(subprocess.check_output([
        'ffprobe','-v','error','-show_entries',
        'stream=codec_name,width,height,nb_frames,r_frame_rate:format=duration,size',
        '-of','json',str(video)]))
    assert int(probe['streams'][0]['nb_frames']) == frame_count
    assert abs(float(probe['format']['duration'])-duration) <= 1/args.fps+.002
    decoded = subprocess.run(['ffmpeg','-v','error','-xerror','-i',str(video),'-f','null','-'],
                             capture_output=True, text=True, check=True)
    assert not decoded.stderr.strip(), decoded.stderr
    save(provenance, {'schema': 'f1_base_review_frame_provenance_v1', 'episode': episode,
        'source_start_ns': str(start), 'fps': args.fps, 'frames': frame_rows,
        'selected_jpeg_sources': {k:list(v.values()) for k,v in selected.items()},
        'timestamp_storage': 'absolute_ns_strings_to_preserve_integer_precision'}, compact=True)
    data_path,native_path = numeric_series(candidate, start, output, slug,args.fps,frame_count)
    raw_sha = sha(raw_path)
    assert fingerprint(raw_path) == candidate['source_fingerprint']
    title,category,focus = DISPLAY[episode]
    entry = {'id': slug, 'episode': episode, 'title':title,'category':category,'status_label':reason,
             'reason': reason, 'file': f'media/{video.name}',
             'video_url':f'media/{video.name}','poster_url':f'media/{poster.name}',
             'data_url':f'data/{data_path.name}',
             'video_duration_s':float(probe['format']['duration']),
             'focus_interval':{'start_s':focus[0],'end_s':focus[1]} if focus else None,
             'poster': f'media/{poster.name}', 'data_file': f'data/{data_path.name}',
             'frames_file': f'data/{provenance.name}', 'evidence_file': f'data/{evidence_path.name}',
             'duration_s': duration, 'source_duration_s': duration, 'fps': args.fps,
             'output_frames': frame_count, 'playback_speed': 1, 'preview_start_s': 0,
             'status': 'candidate_pending_review' if episode in current_sources else 'excluded_confirmed_base_timing',
             'current_772_candidate': episode in current_sources, 'source_accepted': False}
    products = [video, poster, provenance, data_path,native_path]
    evidence = {'schema': 'f1_base_review_media_v1', 'identity': identity,
        'created_at': datetime.now(timezone.utc).isoformat(), 'complete': True,
        'episode': episode, 'raw_path': str(raw_path), 'raw_sha256': raw_sha,
        'raw_source_fingerprint_before_after': candidate['source_fingerprint'],
        'raw_stat_unchanged': True, 'raw_crc_validation': True,
        'candidate_camera_sha256': candidate['camera_npz_sha256'],
        'candidate_signal_sha256': candidate['signal_npz_sha256'],
        'every_camera_log_index_matches_existing_index': True,
        'selected_camera_headers_match_existing_index': True,
        'source_start_ns': str(start), 'source_duration_ns': str(candidate['source_duration_ns']),
        'causal_frame_selection': True, 'no_frames_from_future': True,
        'full_source_interval_preview': True, 'clipped_or_time_shifted': False,
        'exposure_synchronization_claim': False, 'action_labels_modified': False,
        'camera_statistics': stats, 'ffprobe': probe, 'ffmpeg_full_decode_passed': True,
        'output_sha256': {str(p.relative_to(output)): sha(p) for p in products},
        'elapsed_s': time.monotonic()-started, 'manifest_entry': entry}
    save(evidence_path,evidence)
    print(f'Completed {episode}: {duration:.2f}s, {frame_count} frames, {evidence["elapsed_s"]:.1f}s wall',flush=True)
    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'results/20260914/videos/base_review')
    parser.add_argument('--fps',type=int,default=15)
    parser.add_argument('--episode', action='append')
    args = parser.parse_args()
    assert args.fps > 0
    for name in ('media','data'):
        (args.output/name).mkdir(parents=True,exist_ok=True)
    parent = ROOT/'results/demo_775_selection/candidates.jsonl'
    current = ROOT/'results/demo_772_selection/candidates.jsonl'
    candidates = {r['episode']:r for r in rows(parent)}
    current_sources = {r['episode'] for r in rows(current)}
    identity = {'schema':'f1_base_review_media_v1', 'script_sha256':sha(Path(__file__)),
                'parent_candidate_sha256':sha(parent), 'current_candidate_sha256':sha(current),
                'fps':args.fps, 'size':[1920,460], 'sample_sources':[s[0] for s in SAMPLES]}
    run_config = args.output/'media_run_config.json'
    if run_config.exists():
        assert json.loads(run_config.read_text()) == identity, 'Run config differs: preserve old output and use a new destination'
    else:
        save(run_config,identity)
    selected = [(e,r) for e,r in SAMPLES if not args.episode or e in args.episode]
    assert selected
    entries = []
    for episode,reason in selected:
        entries.append(render(candidates[episode],reason,current_sources,args,identity))
        save(args.output/'manifest.json',{'schema':'f1_base_review_v1','episodes':entries,
            'created_at':datetime.now(timezone.utc).isoformat(),
            'notice':'原始三路视频全时长；原底盘为后补弱标签。自动疑点不等于确认错误，未命中不等于已验收。',
            'completed':len(entries),'requested':len(selected),'complete':len(entries)==len(selected),
            'timeline':'seconds_from_mcap_log_origin','fps':args.fps,'identity':identity})
        save(args.output/'media_progress.json',{'completed':len(entries),'requested':len(selected),
             'last_episode':episode,'updated_at':datetime.now(timezone.utc).isoformat()})


if __name__ == '__main__':
    main()

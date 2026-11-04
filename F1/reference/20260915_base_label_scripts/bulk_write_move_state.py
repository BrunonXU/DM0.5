from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import heapq
import json
import math
import os
import shutil
import tempfile
import time
import traceback

import yaml
from mcap.reader import make_reader
from mcap.writer import Writer, CompressionType
from mcap_ros2._dynamic import serialize_dynamic
from mcap_ros2.decoder import DecoderFactory

ROOTS = [Path(r'd:\all_data'), Path(r'd:\all_data_1'), Path(r'd:\all_data_2'), Path(r'd:\all_data_3')]
OUTPUT_ROOTS = {root: root.with_name(root.name + '_derived') for root in ROOTS}
TIMINGS = Path(r'C:\Users\user\AppData\Local\Temp\all_episode_motion_timings.json')
REPORT = Path(r'd:\move_state_bulk_report.json')
LOG = Path(r'd:\move_state_bulk_progress.jsonl')
TOPIC = '/move/State'
SOURCE_TOPIC = '/hal/joint_states'
BACKWARD_M = 0.5
RIGHT_M = 1.6
YAW = 0.0

MSGDEF = '''std_msgs/Header header
geometry_msgs/Twist speed
geometry_msgs/Pose odometry
bool is_soft_auto
bool is_hard_auto
bool is_joystick_on
bool is_handle_on
bool is_parking_mode
bool is_avoid
geometry_msgs/Point[] avoid_points

================================================================================
MSG: std_msgs/Header
builtin_interfaces/Time stamp
string frame_id

================================================================================
MSG: builtin_interfaces/Time
int32 sec
uint32 nanosec

================================================================================
MSG: geometry_msgs/Twist
geometry_msgs/Vector3 linear
geometry_msgs/Vector3 angular

================================================================================
MSG: geometry_msgs/Vector3
float64 x
float64 y
float64 z

================================================================================
MSG: geometry_msgs/Pose
geometry_msgs/Point position
geometry_msgs/Quaternion orientation

================================================================================
MSG: geometry_msgs/Point
float64 x
float64 y
float64 z

================================================================================
MSG: geometry_msgs/Quaternion
float64 x 0
float64 y 0
float64 z 0
float64 w 1
'''
ENCODER = serialize_dynamic('move/msg/State', MSGDEF)['move/msg/State']


def smooth(u: float) -> tuple[float, float]:
    u = min(1.0, max(0.0, u))
    return u * u * (3.0 - 2.0 * u), 6.0 * u * (1.0 - u)


def split_window(start: float, end: float, outbound: bool) -> list[tuple[float, float, float, float, float, float, str]]:
    duration = max(1.2, end - start)
    # Relative durations follow the validated episode 73 trajectory: back, lateral, forward.
    fractions = (0.24, 0.48, 0.28) if outbound else (0.29, 0.41, 0.30)
    a = start
    b = a + duration * fractions[0]
    c = b + duration * fractions[1]
    d = end
    if outbound:
        return [
            (a, b, 0.0, 0.0, -BACKWARD_M, 0.0, 'backward_after_grasp'),
            (b, c, -BACKWARD_M, 0.0, -BACKWARD_M, -RIGHT_M, 'right_translation'),
            (c, d, -BACKWARD_M, -RIGHT_M, 0.0, -RIGHT_M, 'forward_to_bag'),
        ]
    return [
        (a, b, 0.0, -RIGHT_M, -BACKWARD_M, -RIGHT_M, 'backward_from_bag'),
        (b, c, -BACKWARD_M, -RIGHT_M, -BACKWARD_M, 0.0, 'left_translation_return'),
        (c, d, -BACKWARD_M, 0.0, 0.0, 0.0, 'forward_to_origin'),
    ]


def state_at(t: float, segments: list[tuple[float, float, float, float, float, float, str]]) -> tuple[float, float, float, float, str]:
    x = y = vx = vy = 0.0
    phase = 'stationary_at_origin'
    for t0, t1, x0, y0, x1, y1, label in segments:
        if t < t0:
            break
        if t <= t1:
            u = (t - t0) / max(t1 - t0, 1e-9)
            s, ds_du = smooth(u)
            dt = max(t1 - t0, 1e-9)
            return x0 + (x1 - x0) * s, y0 + (y1 - y0) * s, (x1 - x0) * ds_du / dt, (y1 - y0) * ds_du / dt, label
        x, y = x1, y1
        phase = 'stationary_at_bag' if abs(x) < 1e-9 and abs(y + RIGHT_M) < 1e-9 else 'stationary_between_moves'
    if t >= segments[-1][1]:
        phase = 'stationary_at_origin'
    return x, y, 0.0, 0.0, phase


def make_cdr(log_time: int, head_first_ns: int, segments: list[tuple[float, float, float, float, float, float, str]]) -> bytes:
    t = (log_time - head_first_ns) / 1e9
    x, y, vx, vy, _ = state_at(t, segments)
    sec, nanosec = divmod(log_time, 1_000_000_000)
    message = {
        'header': {'stamp': {'sec': int(sec), 'nanosec': int(nanosec)}, 'frame_id': 'base_link'},
        'speed': {'linear': {'x': vx, 'y': vy, 'z': 0.0}, 'angular': {'x': 0.0, 'y': 0.0, 'z': 0.0}},
        'odometry': {'position': {'x': x, 'y': y, 'z': 0.0}, 'orientation': {'x': 0.0, 'y': 0.0, 'z': 0.0, 'w': YAW}},
        'is_soft_auto': False, 'is_hard_auto': True, 'is_joystick_on': False,
        'is_handle_on': False, 'is_parking_mode': False, 'is_avoid': False, 'avoid_points': [],
    }
    return ENCODER(message)


def topic_metadata(count: int) -> dict[str, Any]:
    return {
        'topic_metadata': {
            'name': TOPIC, 'type': 'move/msg/State', 'serialization_format': 'cdr',
            'offered_qos_profiles': [{
                'history': 'keep_last', 'depth': 10, 'reliability': 'reliable', 'durability': 'volatile',
                'deadline': {'sec': 9223372036, 'nsec': 854775807},
                'lifespan': {'sec': 9223372036, 'nsec': 854775807}, 'liveliness': 'automatic',
                'liveliness_lease_duration': {'sec': 9223372036, 'nsec': 854775807},
                'avoid_ros_namespace_conventions': False,
            }], 'type_description_hash': '',
        }, 'message_count': count,
    }


def write_metadata(source: Path, destination: Path, move_count: int, total_count: int) -> None:
    data = yaml.safe_load(source.read_text(encoding='utf-8'))
    info = data['rosbag2_bagfile_information']
    info['message_count'] = total_count
    topics = info['topics_with_message_count']
    topics[:] = [item for item in topics if item['topic_metadata']['name'] != TOPIC]
    topics.append(topic_metadata(move_count))
    for item in info.get('files', []):
        item['message_count'] = total_count
    destination.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding='utf-8')


def already_valid(output_mcap: Path, expected_original: int, expected_move: int) -> bool:
    if not output_mcap.exists() or output_mcap.stat().st_size < 1024:
        return False
    try:
        with output_mcap.open('rb') as stream:
            summary = make_reader(stream).get_summary()
            if summary is None or summary.statistics is None:
                return False
            counts = {channel.topic: summary.statistics.channel_message_counts.get(channel_id, 0) for channel_id, channel in summary.channels.items()}
            return summary.statistics.message_count == expected_original + expected_move and counts.get(TOPIC) == expected_move
    except Exception:
        return False


def process(item: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    source = Path(item['path'])
    source_root = next(root for root in ROOTS if source.is_relative_to(root))
    output_dir = OUTPUT_ROOTS[source_root] / source.parent.name
    output_dir.mkdir(parents=True, exist_ok=True)
    output_mcap = output_dir / source.name
    output_metadata = output_dir / 'metadata.yaml'
    output_info = output_dir / 'estimated_move_state.json'
    with source.open('rb') as stream:
        original_reader = make_reader(stream)
        original_summary = original_reader.get_summary()
        if original_summary is None or original_summary.statistics is None:
            raise RuntimeError('source MCAP has no summary/statistics')
        original_count = original_summary.statistics.message_count
        source_topic_ids = [cid for cid, channel in original_summary.channels.items() if channel.topic == SOURCE_TOPIC]
        move_count = sum(original_summary.statistics.channel_message_counts.get(cid, 0) for cid in source_topic_ids)
    if move_count <= 0:
        raise RuntimeError(f'{SOURCE_TOPIC} has no messages')
    if already_valid(output_mcap, original_count, move_count):
        return {'path': str(source), 'output': str(output_mcap), 'status': 'skipped_valid', 'original_messages': original_count, 'move_messages': move_count, 'elapsed_s': round(time.time() - started, 2), 'confidence': item['windows']['confidence']}

    windows = item['windows']
    segments = split_window(windows['outbound_start_s'], windows['outbound_end_s'], True) + split_window(windows['return_start_s'], windows['return_end_s'], False)
    temp_path = output_mcap.with_suffix('.mcap.partial')
    if temp_path.exists():
        temp_path.unlink()
    with source.open('rb') as source_stream, temp_path.open('wb') as output_stream:
        reader = make_reader(source_stream)
        summary = reader.get_summary()
        writer = Writer(output_stream, compression=CompressionType.ZSTD, enable_crcs=True)
        writer.start(profile='ros2', library='estimated-base-pose bulk merger; mcap-python')
        schema_map = {0: 0}
        for schema_id, schema in summary.schemas.items():
            schema_map[schema_id] = writer.register_schema(schema.name, schema.encoding, schema.data)
        move_schema_id = writer.register_schema('move/msg/State', 'ros2msg', MSGDEF.encode())
        channel_map = {}
        for channel_id, channel in summary.channels.items():
            channel_map[channel_id] = writer.register_channel(channel.topic, channel.message_encoding, schema_map[channel.schema_id], dict(channel.metadata))
        move_channel_id = writer.register_channel(TOPIC, 'cdr', move_schema_id)
        for attachment in reader.iter_attachments():
            writer.add_attachment(attachment.create_time, attachment.log_time, attachment.name, attachment.media_type, attachment.data)
        for metadata in reader.iter_metadata():
            writer.add_metadata(metadata.name, dict(metadata.metadata))
        # The synthetic timestamps exactly follow /hal/joint_states, so inject each immediately after its source message.
        sequence = 0
        copied = 0
        for _, channel, message in reader.iter_messages(log_time_order=True):
            writer.add_message(channel_map[channel.id], message.log_time, message.data, message.publish_time, message.sequence)
            copied += 1
            if channel.topic == SOURCE_TOPIC:
                writer.add_message(move_channel_id, message.log_time, make_cdr(message.log_time, item['head_first_ns'], segments), message.publish_time, sequence)
                sequence += 1
        writer.add_metadata('estimated_base_pose', {
            'status': 'estimated_weak_label_not_measured_odometry', 'source_original': source.name,
            'backward_distance_m': str(BACKWARD_M), 'right_distance_m': str(RIGHT_M), 'yaw_rad': str(YAW),
            'orientation_encoding': 'producer-compatible yaw scalar in odometry.orientation.w',
            'coordinate_convention': '+x forward, +y left', 'timing_method': windows['method'],
            'confidence': str(windows['confidence']),
        })
        writer.finish()
    os.replace(temp_path, output_mcap)
    total_count = original_count + move_count
    write_metadata(source.parent / 'metadata.yaml', output_metadata, move_count, total_count)
    output_info.write_text(json.dumps({
        'status': 'estimated_weak_label_not_measured_odometry', 'source_bag': str(source),
        'output_bag': str(output_mcap), 'source_timestamps_topic': SOURCE_TOPIC,
        'coordinate_convention': '+x forward, +y left', 'orientation_encoding': 'yaw radians directly in odometry.orientation.w',
        'assumptions': {'omnidirectional_base': True, 'backward_distance_m': BACKWARD_M, 'right_distance_m': RIGHT_M, 'yaw_rad': YAW},
        'detected_windows': windows, 'motion_segments': segments, 'samples': move_count,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    if not already_valid(output_mcap, original_count, move_count):
        raise RuntimeError('post-write summary validation failed')
    return {'path': str(source), 'output': str(output_mcap), 'status': 'written', 'original_messages': original_count, 'move_messages': move_count, 'total_messages': total_count, 'elapsed_s': round(time.time() - started, 2), 'confidence': windows['confidence']}


def main() -> None:
    timing_items = json.loads(TIMINGS.read_text(encoding='utf-8'))
    expected_paths = {str(Path(item['path'])) for item in timing_items}
    if len(timing_items) != 801 or len(expected_paths) != 801:
        raise RuntimeError(f'expected 801 unique timing records, got {len(timing_items)}/{len(expected_paths)}')
    for output_root in OUTPUT_ROOTS.values():
        output_root.mkdir(parents=True, exist_ok=True)
    completed: dict[str, dict[str, Any]] = {}
    if LOG.exists():
        for line in LOG.read_text(encoding='utf-8').splitlines():
            try:
                record = json.loads(line)
                if record.get('status') in {'written', 'skipped_valid'}:
                    completed[record['path']] = record
            except Exception:
                pass
    pending = [item for item in timing_items if item['path'] not in completed]
    print(f'total=801 resumed={len(completed)} pending={len(pending)}', flush=True)
    started = time.time()
    with ThreadPoolExecutor(max_workers=2) as pool, LOG.open('a', encoding='utf-8') as log:
        futures = {pool.submit(process, item): item for item in pending}
        for future in as_completed(futures):
            item = futures[future]
            try:
                record = future.result()
            except Exception as exc:
                record = {'path': item['path'], 'status': 'error', 'error': repr(exc), 'traceback': traceback.format_exc(), 'confidence': item['windows']['confidence']}
            completed[record['path']] = record
            log.write(json.dumps(record, ensure_ascii=False) + '\n')
            log.flush()
            count = len(completed)
            if count % 10 == 0 or record['status'] == 'error':
                errors = sum(value['status'] == 'error' for value in completed.values())
                print(f'completed={count}/801 errors={errors} last={record["path"]} elapsed_min={(time.time()-started)/60:.1f}', flush=True)
    records = [completed.get(item['path'], {'path': item['path'], 'status': 'missing'}) for item in timing_items]
    REPORT.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
    errors = sum(record['status'] in {'error', 'missing'} for record in records)
    print(f'DONE total={len(records)} errors={errors} report={REPORT} elapsed_min={(time.time()-started)/60:.1f}', flush=True)


if __name__ == '__main__':
    main()

"""Exercise the real recorder. Create command endpoints, but forbid all sends."""
import json
import sqlite3
import time
from f1_replay.cli import Recorder, select_recording_topics
from f1_replay.recording_check import check_types
from .ros import RosObservationBackend
from .runtime import check_machine, wait_observation


def run(doc, profile, output):
    check_machine(profile)
    backend = RosObservationBackend(profile, doc['client']['camera_topics'])
    recorder = None
    result = {'hardware_published': False, 'control_publishers_created': False,
              'ready': False, 'output': str(output)}
    def forbidden(*args, **kwargs):
        raise RuntimeError('record-check禁止发送任何硬件命令')
    backend._publish_batch = forbidden
    try:
        backend.open()
        wait_observation(backend, doc['client'], profile)
        result['type_check'] = check_types(profile, backend)
        topics, result['diagnostics'] = select_recording_topics(profile, backend)
        backend.enable_commands()
        result['control_publishers_created'] = True
        recorder = Recorder(output, topics)
        start = time.monotonic()
        result['command'] = recorder.start(lambda: backend.poll(0.), backend)
        result['ready_elapsed_s'] = time.monotonic() - start
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            backend.poll(.02)
            recorder.check()
        result['ready'] = True
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        if recorder:
            result['recording'] = recorder.close()
        backend.close()
    if result['ready']:
        counts = {}
        for path in (output/'rosbag').glob('*.db3'):
            with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
                for name, count in db.execute('SELECT topics.name,count(messages.id) FROM topics LEFT JOIN messages ON topics.id=messages.topic_id GROUP BY topics.id'):
                    counts[name] = counts.get(name, 0) + count
        result['message_counts'] = counts
        required = [profile['topics'][k] for k in ('joint_state', 'left_gripper_state', 'right_gripper_state', 'base_state')] + profile['record_topics']
        empty = [t for t in required if not counts.get(t)]
        if empty or result['recording'].get('exit_code') != 0 or not result['recording'].get('metadata_present'):
            result['error'] = f'录包未完整通过：无消息流={empty}; close={result["recording"]}'
    (output/'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    return result

"""Independent tests of the data-label contract; never construct a ROS node."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recording.upstream.lerobot_v3 import FEATURE_NAMES, FEATURE_SIZE
from recording.upstream.sample_history import SampleHistory


def source_history():
    from recording.source_contract import GripperCommandHistory
    return GripperCommandHistory()


LEFT = 'left'
RIGHT = 'right'
GID_A = bytes.fromhex('0102030405060708090a0b0c0d0e0f10')
GID_B = bytes.fromhex('1112131415161718191a1b1c1d1e1f20')
T0 = 1_000_000_000


def primed(side=LEFT):
    history = source_history()
    history.update_writers(side, [GID_A], T0)
    history.observe(side, 73.0, T0 + 1_000_000, GID_A)
    return history


def test_upper16_has_explicit_order_and_no_body_or_base():
    expected = tuple(f'arm_{side}_j{i}' for side in ('l', 'r') for i in range(1, 8))
    expected += ('left_gripper_position', 'right_gripper_position')
    assert FEATURE_SIZE == 16
    assert FEATURE_NAMES == expected


def test_state_history_never_looks_ahead_or_fills_missing_axes():
    history = SampleHistory(['arm_l_j1', 'arm_r_j1'])
    history.add('arm_l_j1', T0, 12)
    history.add('arm_l_j1', T0 + 40_000_000, 34)
    assert history.at('arm_l_j1', T0 + 20_000_000, 50_000_000) == (12, 20_000_000)
    with pytest.raises(ValueError):
        history.at('arm_r_j1', T0 + 20_000_000, 50_000_000)
    with pytest.raises(ValueError):
        history.at('arm_l_j1', T0 + 100_000_000, 50_000_000)


def test_hold_requires_first_actual_command():
    history = source_history()
    history.update_writers(LEFT, [GID_A], T0)
    with pytest.raises(ValueError):
        history.at(LEFT, T0 + 5_000_000)


def test_real_command_hold_keeps_value_and_original_age():
    history = primed()
    history.update_writers(LEFT, [GID_A], T0 + 1_000_000_000)
    held = history.at(LEFT, T0 + 1_000_000_000)
    assert held['value'] == 73.0
    assert held['stamp_ns'] == T0 + 1_000_000
    assert held['age_ns'] == 999_000_000
    assert held['held'] is True
    assert held['writer_gid'] == GID_A.hex()


def test_causal_lookup_does_not_apply_future_gripper_command():
    history = primed()
    history.observe(LEFT, 20.0, T0 + 40_000_000, GID_A)
    before = history.at(LEFT, T0 + 20_000_000)
    after = history.at(LEFT, T0 + 45_000_000)
    assert before['value'] == 73.0 and before['age_ns'] == 19_000_000
    assert after['value'] == 20.0 and after['held'] is False


@pytest.mark.parametrize('writers', [[], [GID_B], [GID_A, GID_B], [bytes(16)]])
def test_disappearing_switching_ambiguous_or_zero_writer_invalidates_hold(writers):
    history = primed()
    history.update_writers(LEFT, writers, T0 + 50_000_000)
    with pytest.raises(ValueError):
        history.at(LEFT, T0 + 55_000_000)


def test_same_gid_reappearance_requires_new_actual_command():
    history = primed()
    history.update_writers(LEFT, [], T0 + 20_000_000)
    history.update_writers(LEFT, [GID_A], T0 + 30_000_000)
    with pytest.raises(ValueError):
        history.at(LEFT, T0 + 31_000_000)
    history.observe(LEFT, 18, T0 + 32_000_000, GID_A)
    assert history.at(LEFT, T0 + 33_000_000)['value'] == 18


def test_writer_clock_rollback_discards_previous_epoch():
    history = primed()
    history.update_writers(LEFT, [GID_A], T0 - 1)
    with pytest.raises(ValueError):
        history.at(LEFT, T0 + 10_000_000)


def test_wrong_publisher_cannot_update_or_retain_previous_hold():
    history = primed()
    with pytest.raises(ValueError):
        history.observe(LEFT, 1.0, T0 + 2_000_000, GID_B)
    with pytest.raises(ValueError):
        history.at(LEFT, T0 + 3_000_000)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -0.01, 100.01])
def test_invalid_gripper_value_invalidates_instead_of_clamping(value):
    history = primed()
    with pytest.raises(ValueError):
        history.observe(LEFT, value, T0 + 2_000_000, GID_A)
    with pytest.raises(ValueError):
        history.at(LEFT, T0 + 3_000_000)


def test_one_gripper_does_not_initialize_the_other():
    history = primed()
    history.update_writers(RIGHT, [GID_B], T0 + 2_000_000)
    with pytest.raises(ValueError):
        history.at(RIGHT, T0 + 3_000_000)
    assert history.at(LEFT, T0 + 3_000_000)['value'] == 73.0


def test_no_future_command_is_used_before_first_observation():
    history = primed()
    with pytest.raises(ValueError):
        history.at(LEFT, T0)


def test_epoch_changes_on_writer_reappearance_even_with_same_gid():
    history = primed()
    previous = history.at(LEFT, T0 + 10_000_000)['epoch']
    history.update_writers(LEFT, [], T0 + 20_000_000)
    history.update_writers(LEFT, [GID_A], T0 + 30_000_000)
    history.observe(LEFT, 18, T0 + 31_000_000, GID_A)
    assert history.at(LEFT, T0 + 32_000_000)['epoch'] > previous


def test_fresh_graph_check_does_not_refresh_command_provenance():
    history = primed()
    previous = history.at(LEFT, T0 + 10_000_000)
    history.update_writers(LEFT, [GID_A], T0 + 900_000_000)
    latest = history.at(LEFT, T0 + 950_000_000)
    assert latest['epoch'] == previous['epoch']
    assert latest['stamp_ns'] == previous['stamp_ns']
    assert latest['age_ns'] == 949_000_000


def test_command_receipt_rollback_cannot_resurrect_previous_target():
    history = primed()
    with pytest.raises(ValueError):
        history.observe(LEFT, 45, T0, GID_A)
    with pytest.raises(ValueError):
        history.at(LEFT, T0 + 50_000_000)


def recorder_method(name):
    """Load actual method bodies without importing or initializing unavailable ROS."""
    import ast
    import numpy as np
    from types import SimpleNamespace
    path = Path(__file__).resolve().parents[1] / 'recording/upstream/lerobot_recorder.py'
    tree = ast.parse(path.read_text())
    node = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == 'LeRobotRecorderNode')
    method = next(item for item in node.body if isinstance(item, ast.FunctionDef) and item.name == name)
    stamp = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == '_message_stamp_ns')
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    program = ast.fix_missing_locations(ast.Module(body=[future, stamp, method], type_ignores=[]))
    namespace = {'np': np, 'time': SimpleNamespace(monotonic=lambda: 10.0, monotonic_ns=lambda: 10_000_000_000),
                 'FEATURE_NAMES': FEATURE_NAMES, 'JOINT_MOTOR_NAMES': FEATURE_NAMES[:14], 'SampleHistory': SampleHistory,
                 'jpeg_dimensions': lambda data: (640, 480)}
    exec(compile(program, str(path), 'exec'), namespace)
    return namespace[name]


def test_gripper_feedback_is_read_by_name_not_position_zero():
    import threading
    from types import SimpleNamespace as NS
    observed = []
    node = NS(_cache_lock=threading.Lock(), _gripper_state=[-1, -1], _gripper_state_time=[0, 0],
              _state_history=NS(add=lambda *args: observed.append(args)),
              get_clock=lambda: NS(now=lambda: NS(nanoseconds=T0)))
    msg = NS(name=['unexpected_joint', 'left_gripper_position'], position=[99, 31])
    recorder_method('_gripper_state_cb')(node, 0, msg)
    assert node._gripper_state == [31, -1]
    assert observed == [('left_gripper_position', T0, 31)]


@pytest.mark.parametrize('names,positions', [(['wrong_name'], [51]),
    (['left_gripper_position','left_gripper_position'], [20, 30]),
    (['left_gripper_position'], [100.1]), (['left_gripper_position'], [float('nan')])])
def test_ambiguous_or_invalid_gripper_feedback_does_not_create_state(names, positions):
    import threading
    from types import SimpleNamespace as NS
    observed = []
    node = NS(_cache_lock=threading.Lock(), _gripper_state=[-1, -1], _gripper_state_time=[0, 0],
              _state_history=NS(add=lambda *args: observed.append(args)),
              get_clock=lambda: NS(now=lambda: NS(nanoseconds=T0)))
    recorder_method('_gripper_state_cb')(node, 0, NS(name=names, position=positions))
    assert node._gripper_state == [-1, -1] and observed == []


@pytest.mark.parametrize('stamp_ns', [0, T0 + 1, T0 - 2_000_000_001])
def test_invalid_camera_source_clock_rejects_instead_of_repairing(stamp_ns):
    from types import SimpleNamespace as NS
    rejected = []
    node = NS(_mode='recording', config={'camera_max_age_s': 2.0},
              get_clock=lambda: NS(now=lambda: NS(nanoseconds=T0)),
              get_logger=lambda: NS(error=lambda message: None),
              _reject_active_episode=lambda reason: rejected.append(reason))
    sec, nano = divmod(stamp_ns, 1_000_000_000)
    msg = NS(data=b'test-image', header=NS(stamp=NS(sec=sec, nanosec=nano)))
    recorder_method('_camera_cb')(node, 'observation.images.base_0_rgb', msg)
    assert len(rejected) == 1
    assert 'clock' in rejected[0] or 'header' in rejected[0]


def staged_dataset(root, task='pick and place', capture='episode-independent-review'):
    """Commit accepts prevalidated media; this unit starts neither encoder nor ROS."""
    import numpy as np
    from recording.upstream.lerobot_v3 import LeRobotV3Dataset, CAMERA_KEYS
    dataset = LeRobotV3Dataset(root, 30, task)
    staging = root / '.staging' / capture
    staging.mkdir(parents=True)
    for key in CAMERA_KEYS:
        (staging / f'{key}.mp4').write_bytes(b'prevalidated-media-placeholder-for-commit-unit-test')
    source = np.array([T0, T0 + 33_333_333], dtype=np.int64)
    arrays = {
        'state': np.tile(np.arange(16, dtype=np.float32), (2, 1)),
        'action': np.tile(np.arange(16, dtype=np.float32) + 25, (2, 1)),
        'timestamp_ns': source,
        'camera_timestamp_ns': np.tile(source[:, None], (1, 3)),
        'camera_integrity_timestamp_ns': np.tile(source[:, None], (1, 3)),
        'capture_monotonic_ns': source,
        'camera_arrival_timestamp_ns': np.tile(source[:, None] + 5_000_000, (1, 3)),
        'state_max_age_ns': np.full(2, 1_000_000, dtype=np.int64),
        'action_max_age_ns': np.full(2, 1_000_000, dtype=np.int64),
        'action_source_timestamp_ns': np.tile(source[:, None] - 1_000_000, (1, 16)),
        'action_source_age_ns': np.full((2, 16), 1_000_000, dtype=np.int64),
        'action_held': np.zeros((2, 16), dtype=bool),
        'gripper_writer_gid': np.tile([GID_A.hex(), GID_B.hex()], (2, 1)),
        'gripper_source_epoch': np.ones((2, 2), dtype=np.int64),
    }
    camera_info = {key: {'width': 640, 'height': 480, 'codec': 'h264', 'fps': 30.0}
                   for key in CAMERA_KEYS}
    return dataset, staging, arrays, camera_info


@pytest.mark.parametrize('fail_file', ['stats.json', 'info.json'])
def test_failed_metadata_commit_retry_cannot_duplicate_episode(tmp_path, monkeypatch, fail_file):
    pytest.importorskip('pyarrow')
    import json
    import recording.upstream.lerobot_v3 as writer
    dataset, staging, arrays, camera_info = staged_dataset(tmp_path / 'branch')
    original = writer._atomic_json
    injected = []
    def failing_once(path, value):
        if path.name == fail_file and not injected:
            injected.append(True)
            raise OSError('independent injected metadata failure')
        return original(path, value)
    monkeypatch.setattr(writer, '_atomic_json', failing_once)
    with pytest.raises(OSError, match='independent injected'):
        dataset.commit(staging, arrays, camera_info)
    assert staging.exists(), 'failed save must retain recoverable staging source'
    episode, _ = dataset.commit(staging, arrays, camera_info)
    rows = writer._read_episode_rows(dataset.root)
    assert episode == 0 and len(rows) == 1 and rows[0]['length'] == 2
    info = json.loads((dataset.root / 'meta/info.json').read_text())
    assert info['total_episodes'] == 1 and info['total_frames'] == 2


def test_changing_task_cannot_relabel_or_leave_pending_transaction(tmp_path):
    pytest.importorskip('pyarrow')
    import json
    import recording.upstream.lerobot_v3 as writer
    dataset, staging, arrays, camera_info = staged_dataset(tmp_path / 'branch', 'original task')
    dataset.commit(staging, arrays, camera_info)
    before = (dataset.root / 'meta/tasks.parquet').read_bytes()
    other, next_staging, arrays, camera_info = staged_dataset(dataset.root, 'unrelated task', 'episode-different-task')
    with pytest.raises(ValueError):
        other.commit(next_staging, arrays, camera_info)
    assert (dataset.root / 'meta/tasks.parquet').read_bytes() == before
    assert len(writer._read_episode_rows(dataset.root)) == 1
    assert all(json.loads(p.read_text())['status'] == 'committed' for p in (dataset.root / '.transactions').glob('*.json'))


def test_missing_video_validation_does_not_poison_next_capture(tmp_path):
    pytest.importorskip('pyarrow')
    import json
    from recording.upstream.lerobot_v3 import CAMERA_KEYS
    dataset, staging, arrays, camera_info = staged_dataset(tmp_path / 'branch')
    (staging / f'{CAMERA_KEYS[-1]}.mp4').unlink()
    with pytest.raises(ValueError):
        dataset.commit(staging, arrays, camera_info)
    assert not list((dataset.root / '.transactions').glob('*.json'))
    second, next_staging, arrays, camera_info = staged_dataset(dataset.root, capture='episode-next-valid')
    assert second.commit(next_staging, arrays, camera_info)[0] == 0


def test_committed_retry_completes_source_archive_after_rename_failure(tmp_path, monkeypatch):
    pytest.importorskip('pyarrow')
    import recording.upstream.lerobot_v3 as writer
    dataset, staging, arrays, camera_info = staged_dataset(tmp_path / 'branch')
    rename = Path.rename
    injected = []
    def failing_once(path, target):
        if path == staging and not injected:
            injected.append(True)
            raise OSError('independent source archive rename failure')
        return rename(path, target)
    monkeypatch.setattr(Path, 'rename', failing_once)
    with pytest.raises(OSError, match='independent source archive'):
        dataset.commit(staging, arrays, camera_info)
    episode, _ = dataset.commit(staging, arrays, camera_info)
    assert episode == 0 and len(writer._read_episode_rows(dataset.root)) == 1
    assert not staging.exists()
    assert (dataset.root / 'source/episode-000000').is_dir()


def test_task_cannot_change_during_retry_before_info_exists(tmp_path, monkeypatch):
    pytest.importorskip('pyarrow')
    import recording.upstream.lerobot_v3 as writer
    dataset, staging, arrays, camera_info = staged_dataset(tmp_path / 'branch', 'original task')
    original = writer._atomic_json
    def fail_info(path, value):
        if path.name == 'info.json':
            raise OSError('independent info failure')
        return original(path, value)
    monkeypatch.setattr(writer, '_atomic_json', fail_info)
    with pytest.raises(OSError):
        dataset.commit(staging, arrays, camera_info)
    assert not (dataset.root / 'meta/info.json').exists()
    monkeypatch.setattr(writer, '_atomic_json', original)
    changed = writer.LeRobotV3Dataset(dataset.root, 30, 'changed task')
    with pytest.raises(ValueError):
        changed.commit(staging, arrays, camera_info)
    assert dataset.commit(staging, arrays, camera_info)[0] == 0


def test_bad_gripper_source_latches_fault_despite_next_good_command():
    import threading
    from types import SimpleNamespace as NS
    sources = primed()
    node = NS(_gripper_sources=sources, _mode='recording', _latched_source_fault=None,
              _cache_lock=threading.Lock(), _gripper_action={}, _gripper_action_time=[0, 0],
              _admit_command=lambda kind, info: {'writer_gid': info.publisher_gid, 'stamp_ns': T0 + 10_000_000},
              get_clock=lambda: NS(now=lambda: NS(nanoseconds=T0 + 10_000_000)))
    callback = recorder_method('_gripper_action_cb')
    callback(node, 0, NS(position=22), NS(publisher_gid=GID_B))
    assert node._latched_source_fault
    old_fault = node._latched_source_fault
    callback(node, 0, NS(position=33), NS(publisher_gid=GID_A))
    assert node._gripper_action[0] == 33
    assert node._latched_source_fault == old_fault


def test_writer_epoch_change_during_recording_is_latched():
    import threading
    from types import SimpleNamespace as NS
    sources = primed()
    sources.update_writers(RIGHT, [GID_B], T0)
    sources.observe(RIGHT, 24, T0 + 1_000_000, GID_B)
    from recording.source_session import PicoSourceEpoch
    proof = PicoSourceEpoch(NS(read=lambda: {'session': 'verified-test-session'}), stable_s=0)
    original_graph = {kind: [{'name': 'pico_upper_bridge' if kind == 'arms' else 'pico_upper_teleop',
                             'gid': GID_A if kind != 'right' else GID_B}] for kind in ('arms', 'left', 'right')}
    assert proof.update(original_graph, T0, 9.0)
    node = NS(_gripper_sources=sources, _mode='recording', _latched_source_fault=None,
              _arm_writer_gid=GID_A, _arm_graph_clock_ns=T0, _pico_source=proof,
              _cache_lock=threading.Lock(), _joint_action={}, _joint_action_time={},
              _action_history=SampleHistory(FEATURE_NAMES),
              get_clock=lambda: NS(now=lambda: NS(nanoseconds=T0 + 20_000_000)),
              get_publishers_info_by_topic=lambda topic: [NS(endpoint_gid=GID_A if topic.endswith('joint_ctl') else GID_B,
                  node_name='pico_upper_bridge' if topic.endswith('joint_ctl') else 'pico_upper_teleop')])
    recorder_method('_refresh_gripper_writers')(node)
    assert node._latched_source_fault
    sources.observe(LEFT, 55, T0 + 25_000_000, GID_B)
    assert node._latched_source_fault


def test_latched_source_fault_cannot_emit_another_dataset_row():
    import threading
    from types import SimpleNamespace as NS
    rejected = []
    sampled = []
    node = NS(_mode='recording', _capture=object(), _mode_lock=threading.RLock(),
              _latched_source_fault='publisher epoch changed',
              _reject_active_episode=lambda reason: rejected.append(reason),
              _ready_snapshot=lambda **kwargs: sampled.append(True))
    assert recorder_method('_sample')(node) is False
    assert rejected == ['publisher epoch changed']
    assert sampled == []


@pytest.mark.parametrize('problem', ['absent_camera', 'single_frame', 'five_hz', 'two_hundred_ms_gap'])
def test_ready_requires_real_multiframe_camera_rate_window(problem):
    import threading
    from types import SimpleNamespace as NS
    from recording.upstream.lerobot_v3 import CAMERA_KEYS
    function = recorder_method('_ready_snapshot')
    function.__globals__['CAMERA_KEYS'] = CAMERA_KEYS
    stamps = [T0 + i * 100_000_000 for i in range(11)]
    observed = {key: [(stamp, 9.0 + i / 10) for i, stamp in enumerate(stamps)] for key in CAMERA_KEYS}
    cameras = {key: (b'test-jpeg', stamps[-1], 10_000_000_000, 640, 480, 11, stamps[-1]) for key in CAMERA_KEYS}
    if problem == 'absent_camera':
        cameras.pop(CAMERA_KEYS[1])
    elif problem == 'single_frame':
        observed[CAMERA_KEYS[1]] = [(stamps[-1], 10.0)]
    elif problem == 'five_hz':
        observed[CAMERA_KEYS[1]] = [(T0 + i * 200_000_000, 9.0 + i / 5) for i in range(6)]
    else:
        seconds = [0, .05, .10, .20, .30, .50, .60, .70, .80, .90, 1.0]
        observed[CAMERA_KEYS[1]] = [(T0 + int(t * 1e9), 9.0 + t) for t in seconds]
    node = NS(config={'state_max_age_s': .05, 'camera_max_age_s': 2.0, 'camera_max_frame_gap_s': .15,
                      'fps': 10, 'max_rate_error_ratio': .05},
              _last_graph_check_monotonic=10., _arm_writer_gid=GID_A, _cache_lock=threading.Lock(),
              _joint_state=dict.fromkeys(FEATURE_NAMES[:14], 1.), _joint_state_time=dict.fromkeys(FEATURE_NAMES[:14], 10.),
              _gripper_state_time=[10.,10.], _cameras=cameras, _observed_camera_stamps=observed,
              get_clock=lambda: NS(now=lambda: NS(nanoseconds=T0 + 1_000_000_000)))
    snapshot, reason, fatal = function(node)
    assert snapshot is None and reason and 'camera' in reason
    assert fatal is False

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as Namespace
import sys

import pytest

from f1_harness.boot_capture import BODY_HEAD_NAMES, BootSamples, capture_boot_recheck


def info():
    return Namespace(width=640, height=480, header=Namespace(frame_id='camera'),
        distortion_model='plumb_bob', k=[1.] * 9, d=[0.] * 5,
        r=[0.] * 9, p=[0.] * 12)


def joints():
    return Namespace(name=list(BODY_HEAD_NAMES), position=[0.] * 8,
        header=Namespace(stamp=Namespace(sec=1, nanosec=0)))


def fill(samples, now=1_000_000_000):
    for camera in ('head', 'right'):
        for stream in ('rgb', 'depth'):
            samples.camera(camera, stream, info(), now)
    samples.joint(joints(), now - 300_000_000)
    samples.joint(joints(), now)


def test_capture_requires_window_freshness_and_all_four_infos():
    samples = BootSamples()
    fill(samples)
    assert samples.complete(1_000_000_000)
    assert not samples.complete(1_600_000_000)
    del samples.info[('head', 'depth')]
    assert not samples.complete(1_000_000_000)


def test_camera_info_changes_invalidate_the_capture_window():
    samples = BootSamples()
    fill(samples)
    changed = info()
    changed.k[0] = 2.
    samples.camera('right', 'rgb', changed, 1_000_000_001)
    assert not samples.complete(1_000_000_001)
    assert samples.fields()['camera_info_changes'] == ['right:rgb']


def test_feedback_must_have_unique_complete_finite_joint_mapping():
    samples = BootSamples()
    message = joints()
    message.position[2] = float('nan')
    with pytest.raises(ValueError, match='feedback_missing'):
        samples.joint(message, 1)
    message = joints()
    message.name[2] = message.name[1]
    with pytest.raises(ValueError, match='mapping_invalid'):
        samples.joint(message, 2)


@pytest.fixture
def site(tmp_path, monkeypatch):
    def write(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value))
        return path
    cal = write('coarse.json', dict(boot_id='old', calibration_data_id='cal'))
    model = write('mapping.json', {})
    write('development.local.json', dict(calibration=dict(path=cal.name,
        sha256=hashlib.sha256(cal.read_bytes()).hexdigest()),
        task_hash='a' * 64, model=dict(urdf_path=str(model), mapping_path=str(model))))
    write('gateway.local.json', dict(observation={}, scene={}))
    monkeypatch.setattr('f1_harness.boot_capture._boot_id', lambda: 'current')
    monkeypatch.setattr('f1_harness.boot_capture._collect_native', lambda _: dict(
        complete_window=True, errors=[], observed_monotonic_ns=1))
    def build(raw, evidence_path, **kwargs):
        assert Path(evidence_path).is_file()
        assert json.loads(Path(evidence_path).read_text()) == raw
        assert kwargs['task_hash'] == 'a' * 64
        return dict(schema='test-receipt', evidence=str(evidence_path), boot=raw['current_boot_id'])
    monkeypatch.setattr('f1_harness.boot_recheck.build_boot_receipt', build)
    return tmp_path


def test_capture_writes_new_separate_receipt_without_changing_calibration(site):
    original = (site / 'coarse.json').read_bytes()
    result = capture_boot_recheck(site, site)
    raw = json.loads((site / 'boot-recheck-evidence.json').read_text())
    assert raw['current_boot_id'] == raw['finished_boot_id'] == 'current'
    assert raw['capture_boot_id'] == 'old'
    assert not raw['motion_authorized'] and raw['command_publishers'] == 0
    assert result['boot'] == 'current'
    assert (site / 'coarse.json').read_bytes() == original
    with pytest.raises(FileExistsError):
        capture_boot_recheck(site, site)


def test_reboot_during_capture_preserves_evidence_but_cannot_issue_receipt(site, monkeypatch):
    boots = iter(['first', 'second'])
    monkeypatch.setattr('f1_harness.boot_capture._boot_id', lambda: next(boots))
    with pytest.raises(ValueError, match='boot_changed_during_recheck'):
        capture_boot_recheck(site, site)
    assert (site / 'boot-recheck-evidence.json').is_file()
    assert not (site / 'boot-recheck.json').exists()


def test_incomplete_capture_preserves_failure_evidence_and_has_no_receipt(site, monkeypatch):
    monkeypatch.setattr('f1_harness.boot_capture._collect_native', lambda _: dict(
        complete_window=False, errors=['camera unavailable'], observed_monotonic_ns=1))
    with pytest.raises(ValueError, match='camera unavailable'):
        capture_boot_recheck(site, site)
    assert (site / 'boot-recheck-evidence.json').is_file()
    assert not (site / 'boot-recheck.json').exists()


def test_validator_rejection_preserves_evidence_and_has_no_receipt(site, monkeypatch):
    def reject(*args, **kwargs):
        raise ValueError('body_head_changed')
    monkeypatch.setattr('f1_harness.boot_recheck.build_boot_receipt', reject)
    with pytest.raises(ValueError, match='body_head_changed'):
        capture_boot_recheck(site, site)
    assert (site / 'boot-recheck-evidence.json').is_file()
    assert not (site / 'boot-recheck.json').exists()


def test_native_capture_spins_executor_bound_to_its_private_context(monkeypatch):
    from f1_harness.boot_capture import _collect_native
    calls = []
    class Context:
        active = False
        def ok(self):
            return self.active
    class Node:
        def create_subscription(self, *args):
            return object()
        def get_service_names_and_types(self):
            return []
        def destroy_node(self):
            calls.append('node_destroy')
    node = Node()
    def init(*, args, context):
        context.active = True
        calls.append(('init', context))
    def create_node(name, **kwargs):
        assert kwargs['context'] is calls[0][1]
        return node
    def shutdown(*, context):
        context.active = False
        calls.append('context_shutdown')
    def forbidden_global_spin(*args, **kwargs):
        raise AssertionError('global executor must never be used')
    class Executor:
        def __init__(self, *, context):
            assert context is calls[0][1]
        def add_node(self, value):
            assert value is node
        def spin_once(self, *, timeout_sec):
            calls.append('explicit_spin')
            raise RuntimeError('stop_after_explicit_executor')
        def shutdown(self, *, timeout_sec):
            calls.append('executor_shutdown')
    modules = {
        'rclpy': Namespace(init=init, create_node=create_node, shutdown=shutdown,
                           spin_once=forbidden_global_spin),
        'rclpy.context': Namespace(Context=Context),
        'rclpy.executors': Namespace(SingleThreadedExecutor=Executor),
        'rclpy.qos': Namespace(qos_profile_sensor_data=object()),
        'sensor_msgs.msg': Namespace(CameraInfo=object, JointState=object),
        'rosidl_runtime_py.utilities': Namespace(get_service=lambda name: object),
        'rosidl_runtime_py.convert': Namespace(message_to_ordereddict=lambda value: value),
    }
    for name, value in modules.items():
        monkeypatch.setitem(sys.modules, name, value)
    profile = dict(cameras=[dict(name=camera, rgb=dict(camera_info_topic='/'+camera+'/rgb'),
        depth=dict(camera_info_topic='/'+camera+'/depth')) for camera in ('head', 'right')])
    result = _collect_native(profile)
    assert result['errors'] == ['RuntimeError:stop_after_explicit_executor']
    assert calls[1:] == ['explicit_spin', 'executor_shutdown', 'node_destroy', 'context_shutdown']


def test_reused_session_still_passes_per_task_receipt_validation(site,monkeypatch):
    from f1_harness.boot_capture import NativeBootSession
    session=NativeBootSession.__new__(NativeBootSession);session.profile={};calls=[]
    def collect():
        calls.append(True)
        return dict(complete_window=True,errors=[],observed_monotonic_ns=len(calls))
    session.collect=collect
    first=capture_boot_recheck(site,site/'capture-one',session=session)
    second=capture_boot_recheck(site,site/'capture-two',session=session)
    assert len(calls)==2 and first['evidence']!=second['evidence']
    assert json.loads((site/'capture-one/boot-recheck-evidence.json').read_text())['observed_monotonic_ns']==1
    assert json.loads((site/'capture-two/boot-recheck-evidence.json').read_text())['observed_monotonic_ns']==2
    def reject(*args,**kwargs):raise ValueError('fresh_reference_changed')
    monkeypatch.setattr('f1_harness.boot_recheck.build_boot_receipt',reject)
    with pytest.raises(ValueError,match='fresh_reference_changed'):
        capture_boot_recheck(site,site/'rejected',session=session)
    assert not (site/'rejected/boot-recheck.json').exists()


def test_session_cannot_collect_for_different_camera_profile(site):
    from f1_harness.boot_capture import NativeBootSession
    session=NativeBootSession.__new__(NativeBootSession);session.profile={'different':True}
    with pytest.raises(ValueError,match='profile_mismatch'):
        capture_boot_recheck(site,site/'wrong-profile',session=session)
    assert not (site/'wrong-profile').exists()

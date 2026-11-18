import copy
import json
import struct
import sys
from types import SimpleNamespace as Namespace

import pytest
from pydantic import BaseModel, ValidationError

from f1_harness.contracts import Observation, MeasuredGripperFeedback, digest
from f1_harness.gripper_feedback import PassiveCanGripper, decode_status
from f1_harness.ros_observer import RosObserver


def feedback():
    reader = PassiveCanGripper(interface='can1', response_id=0x301)
    value = reader.snapshot()
    packet = struct.pack('=IBBBB8B', 0x301, 8, 5, 0, 0, 0, 0, 73, 0, 4, 0, 0, 0) + b'\0' * 56
    row = decode_status(packet, 0, response_id=0x301, source_instance=reader.source_instance,
        sequence=1, received_ns=1000, received_realtime_ns=10000, kernel_received_realtime_ns=9900)
    value.update(running=True, received_count=1, latest=row, samples=[row])
    return value


def observation(**kwargs):
    return Observation(observation_id='obs', acquisition_instance='camera', received_ns=1000,
                       clock_domain='host', state16=(0.,)*16, **kwargs)


def test_optional_measurement_preserves_old_signed_and_nested_serialization():
    old = observation()
    original = old.model_dump(mode='json')
    assert 'measured_gripper' not in original
    assert 'measured_gripper' not in json.loads(old.model_dump_json())
    assert Observation.model_validate(original).model_dump(mode='json') == original
    assert digest(old) == digest(original)
    class Envelope(BaseModel):
        observation: Observation
    nested = Envelope(observation=old)
    assert 'measured_gripper' not in nested.model_dump()['observation']
    assert json.loads(nested.model_dump_json())['observation'] == original
    enriched = observation(measured_gripper=MeasuredGripperFeedback.model_validate(feedback()))
    assert enriched.state16 == old.state16
    assert enriched.measured_gripper.latest.closure_percent == pytest.approx(18200/255)
    assert Observation.model_validate_json(enriched.model_dump_json()) == enriched
    assert digest(enriched) != digest(old)


@pytest.mark.parametrize('change', ['epoch', 'duplicate', 'forged_value', 'invalid_time', 'wrong_unit', 'counter'])
def test_malformed_measurement_cannot_be_typed_as_independent_evidence(change):
    value = copy.deepcopy(feedback())
    if change == 'epoch':value['source_instance'] = 'different-reader'
    if change == 'duplicate':value['samples'].append(copy.deepcopy(value['latest']))
    if change == 'forged_value':value['latest']['closure_percent'] = 100.
    if change == 'invalid_time':value['latest']['kernel_received_realtime_ns'] = None
    if change == 'wrong_unit':value['position_unit'] = 'radians'
    if change == 'counter':value['received_count'] = 0
    with pytest.raises(ValidationError):MeasuredGripperFeedback.model_validate(value)


def fake_ros(monkeypatch):
    calls = []
    class Context:
        active = False
        def ok(self):return self.active
    class Node:
        def destroy_node(self):calls.append('node_destroy')
    def init(*, args, context):context.active = True
    def shutdown(*, context):context.active = False;calls.append('context_shutdown')
    class Executor:
        def __init__(self, **_):pass
        def add_node(self, node):pass
        def shutdown(self, **_):calls.append('executor_shutdown')
    modules = {
        'rclpy':Namespace(init=init, create_node=lambda *a, **kw:Node(), shutdown=shutdown),
        'rclpy.context':Namespace(Context=Context),
        'rclpy.executors':Namespace(SingleThreadedExecutor=Executor),
        'rclpy.qos':Namespace(qos_profile_sensor_data=object()),
        'sensor_msgs.msg':Namespace(CameraInfo=object, JointState=object, Image=object, CompressedImage=object)}
    for name, module in modules.items():monkeypatch.setitem(sys.modules, name, module)
    return calls


def test_ros_lifecycle_attaches_independent_feedback_without_replacing_state(monkeypatch):
    calls = fake_ros(monkeypatch)
    value = feedback()
    class Reader:
        def __init__(self, **kwargs):assert kwargs['interface'] == 'can1'
        def start(self):calls.append('reader_start');return True
        def snapshot(self):return value
        def close(self):calls.append('reader_close')
    monkeypatch.setattr('f1_harness.gripper_feedback.PassiveCanGripper', Reader)
    raw = RosObserver({'cameras':[], 'measured_gripper':dict(interface='can1', response_id=0x301)}, Namespace())
    raw.buffer.add_state([100.] * 16, [True] * 16, 1000)
    obs = raw.observe()
    assert obs.state16[15] == 100.
    assert obs.measured_gripper.latest.raw_position == 73
    raw.close();raw.close()
    assert calls == ['reader_start', 'reader_close', 'executor_shutdown', 'node_destroy', 'context_shutdown']


def test_reader_unavailable_is_quality_warning_without_startup_failure(monkeypatch):
    calls = fake_ros(monkeypatch)
    value = feedback();value.update(running=False, error='OSError:19', received_count=0, latest=None, samples=[])
    class Reader:
        def __init__(self, **kwargs):pass
        def start(self):return False
        def snapshot(self):return value
        def close(self):calls.append('reader_close')
    monkeypatch.setattr('f1_harness.gripper_feedback.PassiveCanGripper', Reader)
    raw = RosObserver({'cameras':[], 'measured_gripper':dict(interface='can1', response_id=0x301)}, Namespace())
    obs = raw.observe()
    assert not obs.measured_gripper.running and obs.measured_gripper.latest is None
    assert 'measured_gripper_unavailable' in obs.quality_warnings
    assert not obs.faults
    raw.close()


@pytest.mark.parametrize('config', [None, {'enabled':False}])
def test_disabled_observer_does_not_construct_socket_or_change_serialization(monkeypatch, config):
    fake_ros(monkeypatch)
    def unexpected(**kwargs):raise AssertionError('disabled reader constructed')
    monkeypatch.setattr('f1_harness.gripper_feedback.PassiveCanGripper', unexpected)
    raw = RosObserver({'cameras':[], 'measured_gripper':config}, Namespace())
    assert 'measured_gripper' not in raw.observe().model_dump()
    raw.close()


def test_invalid_optional_configuration_does_not_stop_camera_observer(monkeypatch):
    fake_ros(monkeypatch)
    raw = RosObserver({'cameras':[], 'measured_gripper':'bad'}, Namespace())
    obs = raw.observe()
    assert obs.measured_gripper is None
    assert 'measured_gripper_unavailable' in obs.quality_warnings
    assert not obs.faults
    raw.close()

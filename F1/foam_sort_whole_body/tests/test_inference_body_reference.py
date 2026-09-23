"""Native reference is not old demonstration angles or an instruction to move."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'robot'))
from f1_policy_client import body_reference as ref
from f1_policy_client.config import load_config
from f1_replay.bundle import BODY_NAMES
from f1_replay.runtime import ReplayError


def samples():
    return [{'received_ns': [i * 50_000_000] * 5,
             'positions_deg': [0., 0., -15., -42., -28.]} for i in range(21)]


@pytest.mark.parametrize('bad', ['moving', 'duplicate', 'short', 'nan'])
def test_unstable_or_stale_reference_is_rejected(bad):
    s = samples()
    if bad == 'moving': s[-1]['positions_deg'][2] += .11
    if bad == 'duplicate': s[-1]['received_ns'] = s[-2]['received_ns']
    if bad == 'short': s.pop()
    if bad == 'nan': s[-1]['positions_deg'][0] = float('nan')
    with pytest.raises(ReplayError): ref.summarize(s)


def test_reference_cannot_be_used_on_another_robot_or_changed_without_evidence():
    s = samples()
    body = {'machine_id': 'D071', 'samples': s, 'samples_sha256': ref.digest(s),
            'body_positions_deg': ref.summarize(s)}
    ref.validate_reference(body, {'expected_machine_id': 'D071'})
    with pytest.raises(ReplayError): ref.validate_reference(body, {'expected_machine_id': 'D070'})
    body['body_positions_deg'][2] = -42
    with pytest.raises(ReplayError): ref.validate_reference(body, {'expected_machine_id': 'D071'})


def test_readonly_capture_retains_previous_reference_and_never_enables_publishers(tmp_path, monkeypatch):
    doc, profile = load_config()
    profile['expected_machine_id'] = 'D071'
    target = tmp_path / 'body.json'
    target.write_text('previous reference')
    doc['client']['body_reference'] = str(target)
    monkeypatch.setattr(ref, 'REFERENCE_ROOT', tmp_path)
    monkeypatch.setattr(ref, 'check_machine', lambda p: None)
    clock, instances = [1_000_000_000], []
    monkeypatch.setattr(ref.time, 'monotonic_ns', lambda: clock[0])
    monkeypatch.setattr(ref.time, 'monotonic', lambda: clock[0] / 1e9)
    class ReadOnlyBackend:
        def __init__(self, p): instances.append(self); self.closed = False
        def open(self): pass
        def close(self): self.closed = True
        def poll(self, wait): clock[0] += 50_000_000
        def snapshot(self):
            return {'joints': {n: {'position': float(i), 'received_ns': clock[0]}
                               for i, n in enumerate(BODY_NAMES)},
                    'base': {'received_ns': clock[0], 'vx': 0., 'vy': 0., 'wz': 0.}}
    result = ref.capture(doc, profile, tmp_path / 'config.json', backend_factory=ReadOnlyBackend)
    assert not result['hardware_published'] and not result['control_publishers_created']
    assert instances[0].closed
    assert [p.read_text() for p in tmp_path.glob('body_previous_*.json')] == ['previous reference']
    body = json.loads(target.read_text())
    ref.validate_reference(body, profile)
    assert body['body_positions_deg'] == [0., 1., 2., 3., 4.]


def test_new_machine_config_requires_native_reference_but_can_capture_without_one(tmp_path):
    task = Path(__file__).resolve().parents[1]
    doc = json.loads((task / 'configs/inference.5k.json').read_text())
    doc['client']['runtime_profile'] = str(task / 'configs/replay_f1.json')
    doc['client']['profile_overrides']['expected_machine_id'] = 'D071'
    doc['client']['body_reference'] = str(tmp_path / 'not_captured.json')
    path = tmp_path / 'inference.json'
    path.write_text(json.dumps(doc))
    load_config(path, require_body_reference=False)
    with pytest.raises(ReplayError, match='capture-body'): load_config(path)
    doc['client']['body_reference'] = str(task / 'configs/inference_body_reference.json')
    path.write_text(json.dumps(doc))
    with pytest.raises(ReplayError, match='不能使用旧示教角度'): load_config(path)

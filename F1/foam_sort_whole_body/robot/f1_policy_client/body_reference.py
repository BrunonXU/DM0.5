"""Bind a stable native body pose through subscriptions only; never reset the robot."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import time
import uuid

from f1_replay.bundle import BODY_NAMES
from f1_replay.runtime import ReplayError, base_stopped, fresh_value, finite_number

REFERENCE_ROOT = Path('/home/user/Bruno')


def check_machine(profile):
    expected = profile.get('expected_machine_id')
    if expected and Path('/etc/machine-id').read_text().strip() != expected:
        raise ReplayError('本机machine-id与机器人配置不一致')


def digest(samples):
    return hashlib.sha256(json.dumps(samples, sort_keys=True, allow_nan=False).encode()).hexdigest()


def summarize(samples):
    if len(samples) < 20:
        raise ReplayError('身体参考需要至少20个不同反馈样本')
    for sample in samples:
        if (len(sample['received_ns']) != 5 or len(sample['positions_deg']) != 5
                or any(type(t) is not int or t < 0 for t in sample['received_ns'])
                or not all(finite_number(v) for v in sample['positions_deg'])):
            raise ReplayError('身体参考包含无效反馈')
    for i, name in enumerate(BODY_NAMES):
        times = [s['received_ns'][i] for s in samples]
        if any(b <= a for a, b in zip(times, times[1:])) or times[-1] - times[0] < 1_000_000_000:
            raise ReplayError(f'{name}需要至少1秒递增反馈')
    values = [[s['positions_deg'][i] for s in samples] for i in range(5)]
    if any(max(v) - min(v) > .1 for v in values):
        raise ReplayError('身体仍在运动或抖动；保持稳定后重新 capture-body')
    return [statistics.median(v) for v in values]


def validate_reference(body, profile):
    if (not profile.get('expected_machine_id')
            or body.get('machine_id') != profile['expected_machine_id']):
        raise ReplayError('身体参考与本机身份不一致')
    try:
        samples = body['samples']
        if digest(samples) != body['samples_sha256'] or summarize(samples) != body['body_positions_deg']:
            raise ReplayError('身体参考与原始稳定采样不一致')
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ReplayError(f'身体参考采样证据无效：{exc}') from exc


def capture(doc, profile, config_path, *, backend_factory=None):
    check_machine(profile)
    if not profile.get('expected_machine_id'):
        raise ReplayError('capture-body需要绑定本机machine-id的配置')
    path = Path(doc['client']['body_reference']).expanduser()
    if not path.is_absolute():
        path = Path(config_path).resolve().parent / path
    path = path.resolve()
    if not path.is_relative_to(REFERENCE_ROOT):
        raise ReplayError('原生身体参考只能保存到/home/user/Bruno内')
    if backend_factory is None:
        from f1_replay.ros_backend import RosBackend
        backend_factory = RosBackend
    backend = backend_factory(profile)
    samples = []
    try:
        backend.open()
        deadline = time.monotonic() + doc['client']['feedback_wait_s']
        while time.monotonic() < deadline:
            backend.poll(.02)
            snapshot, now = backend.snapshot(), time.monotonic_ns()
            try:
                positions = [fresh_value(snapshot.get('joints', {}).get(n), now,
                             profile['feedback_max_age_s'] * 1e9, n) for n in BODY_NAMES]
                if not base_stopped(snapshot, profile, now):
                    raise ReplayError('采样期间底盘必须静止')
            except ReplayError:
                samples.clear()
                continue
            times = [snapshot['joints'][n]['received_ns'] for n in BODY_NAMES]
            if samples and any(b <= a for a, b in zip(samples[-1]['received_ns'], times)):
                continue
            samples.append({'positions_deg': positions, 'received_ns': times})
            if len(samples) >= 20 and all(b - a >= 1_000_000_000 for a, b in zip(samples[0]['received_ns'], times)):
                positions = summarize(samples)
                break
        else:
            raise ReplayError('未取得1秒新鲜静止身体反馈；未保存参考')
    finally:
        backend.close()
    body = {'schema': 'f1_native_body_reference_v1',
            'machine_id': profile['expected_machine_id'], 'robot_name': profile.get('robot_name'),
            'captured_utc': datetime.now(timezone.utc).isoformat(),
            'body_joint_names': list(BODY_NAMES), 'body_positions_deg': positions,
            'samples': samples, 'samples_sha256': digest(samples),
            'hardware_published': False,
            'scope': 'Onsite selected native pose; no physical equivalence to dataset claimed'}
    validate_reference(body, profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep every capture immutable; replace the selected file atomically.
    archive = path.with_name(path.stem + '_' + uuid.uuid4().hex + '.json')
    archive.write_text(json.dumps(body, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
    if path.exists():
        backup = path.with_name(path.stem + '_previous_' + uuid.uuid4().hex + '.json')
        backup.write_bytes(path.read_bytes())
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    temporary.write_bytes(archive.read_bytes())
    os.replace(temporary, path)
    return {'body_reference': str(path), 'archive': str(archive),
            'body_positions_deg': positions, 'hardware_published': False,
            'control_publishers_created': False}

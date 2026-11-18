"""Bind a new boot to fresh read-only measurements without rewriting calibration."""
from pathlib import Path
import hashlib
import json
import math


SCHEMA = 'f1-harness-boot-recheck-v1'
CAPTURE_SCHEMA = 'f1-harness-boot-capture-v1'
MEASUREMENT_KEYS = (
    'current_boot_id', 'observed_monotonic_ns', 'synthetic', 'command_publishers',
    'motion_authorized', 'precision_certified', 'calibration_sha256',
    'calibration_data_id', 'capture_boot_id', 'model', 'camera_info',
    'camera_serials', 'body_head_deg',
)
BODY_HEAD_NAMES = tuple('head_j' + str(i) for i in range(1, 4)) + tuple(
    'body_j' + str(i) for i in range(1, 6))
INFO_KEYS = ('k', 'd', 'r', 'p', 'width', 'height', 'frame_id', 'distortion_model')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _file(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 512 * 1024:
        raise ValueError('boot_recheck_regular_bounded_evidence_required')
    data = path.read_bytes()
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError('boot_recheck_evidence_object_required')
    return value, hashlib.sha256(data).hexdigest()


def calibration_serials(calibration):
    """Read the original camera identities; no hostname or topic-name substitutions."""
    result = {}
    for camera, prefix in (('head', '/camera/head/'), ('right', '/camera/right_wrist/')):
        rows = [row for row in calibration.get('camera_device_info', ())
                if isinstance(row, dict) and str(row.get('service', '')).startswith(prefix)]
        if len(rows) != 1:
            raise ValueError('boot_recheck_original_device_identity_required:' + camera)
        payload = rows[0].get('result', {})
        if payload.get('success') is False:
            raise ValueError('boot_recheck_original_device_identity_required:' + camera)
        serial = payload.get('info', payload).get('serial_number')
        if not isinstance(serial, str) or not serial:
            raise ValueError('boot_recheck_original_device_identity_required:' + camera)
        result[camera] = serial
    return result


def canonical_camera_info(value):
    if not isinstance(value, dict) or any(key not in value for key in INFO_KEYS):
        raise ValueError('boot_recheck_camera_info_incomplete')
    return {key: value[key] for key in INFO_KEYS}


def _check_measurements(value, *, calibration, calibration_sha256, scene,
                        current_boot_id, now_ns):
    if (value.get('synthetic') is not False or value.get('motion_authorized') is not False
            or value.get('precision_certified') is not False
            or type(value.get('command_publishers')) is not int or value['command_publishers'] != 0):
        raise ValueError('boot_recheck_read_only_real_capture_required')
    if (not isinstance(current_boot_id, str) or not current_boot_id
            or value.get('current_boot_id') != current_boot_id
            or value.get('capture_boot_id') != calibration.get('boot_id')
            or value.get('calibration_sha256') != calibration_sha256
            or value.get('calibration_data_id') != calibration.get('calibration_data_id')):
        raise ValueError('boot_recheck_boot_or_calibration_binding_mismatch')
    observed = value.get('observed_monotonic_ns')
    if type(observed) is not int or not 0 < observed <= now_ns:
        raise ValueError('boot_recheck_capture_time_invalid')
    # This is a per-boot compatibility receipt. Per-frame freshness and live
    # body/camera checks remain in the existing observation admission path.
    model = value.get('model')
    if not isinstance(model, dict) or any(
            model.get(key) != calibration.get('executor_identity', {}).get(key)
            or not isinstance(model.get(key), str) or len(model[key]) != 64
            for key in ('urdf_sha256', 'mapping_sha256')):
        raise ValueError('boot_recheck_model_identity_changed')
    serials = calibration_serials(calibration)
    if value.get('camera_serials') != serials:
        raise ValueError('boot_recheck_camera_device_changed')
    for camera in ('head', 'right'):
        for stream in ('rgb', 'depth'):
            actual = value.get('camera_info', {}).get(camera, {}).get(stream)
            expected = calibration.get('cameras', {}).get(camera, {}).get(stream + '_CameraInfo')
            if _canonical(canonical_camera_info(actual)) != _canonical(canonical_camera_info(expected)):
                raise ValueError('boot_recheck_camera_info_changed:' + camera + ':' + stream)
    tolerance = scene.get('reference_tolerance_deg')
    actual = value.get('body_head_deg')
    reference = calibration.get('body_head_reference_deg', {})
    if (isinstance(tolerance, bool) or not isinstance(tolerance, (int, float))
            or not math.isfinite(tolerance) or tolerance <= 0
            or not isinstance(actual, dict) or set(actual) != set(BODY_HEAD_NAMES)):
        raise ValueError('boot_recheck_body_head_measurement_required')
    differences = {}
    from .head_kinematics import checked_head_model
    head_model=checked_head_model(scene,calibration)
    for name in BODY_HEAD_NAMES:
        before, measured = reference.get(name), actual[name]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in (before, measured)):
            raise ValueError('boot_recheck_body_head_measurement_invalid:' + name)
        differences[name] = measured - before
        if abs(differences[name]) > tolerance and head_model is None:
            raise ValueError('boot_recheck_body_head_reference_changed:' + name +
                             ':delta_deg=' + format(differences[name], '.6g') +
                             ':allowed_deg=' + format(tolerance, '.6g'))
    checks=dict(body_head_delta_deg=differences, reference_tolerance_deg=tolerance,
                camera_serials=serials, precision_certified=False, motion_authorized=False)
    if head_model is not None:
        checks.update(head_pose_mode='measured_kinematics',
            T_world_head_camera=head_model.pose(actual).tolist(),
            reference_drift_warning=any(abs(v)>tolerance for v in differences.values()))
    return checks


def _check_capture_window(raw, *, calibration, scene):
    from .head_kinematics import checked_head_model
    compensated=checked_head_model(scene,calibration) is not None
    observed = raw['observed_monotonic_ns']
    start = raw.get('window_start_received_ns')
    if (raw.get('complete_window') is not True or raw.get('errors') != []
            or raw.get('finished_boot_id') != raw['current_boot_id']
            or type(start) is not int or not 0 < start < observed):
        raise ValueError('boot_recheck_complete_capture_window_required')
    if raw.get('camera_info_changes') != []:
        raise ValueError('boot_recheck_camera_info_changed_during_capture')
    for camera in ('head', 'right'):
        for stream in ('rgb', 'depth'):
            stamp = raw.get('camera_info_received_ns', {}).get(camera, {}).get(stream)
            if type(stamp) is not int or not start <= stamp <= observed or observed-stamp > 500_000_000:
                raise ValueError('boot_recheck_camera_info_not_fresh:' + camera + ':' + stream)
    samples = raw.get('joint_samples')
    if not isinstance(samples, list) or not 2 <= len(samples) <= 4096:
        raise ValueError('boot_recheck_joint_window_required')
    previous = start - 1
    for sample in samples:
        stamp = sample.get('received_ns') if isinstance(sample, dict) else None
        if type(stamp) is not int or not previous < stamp <= observed:
            raise ValueError('boot_recheck_joint_window_time_invalid')
        previous = stamp
        body = sample.get('body_head_deg')
        if not isinstance(body, dict) or set(body) != set(BODY_HEAD_NAMES):
            raise ValueError('boot_recheck_joint_window_mapping_invalid')
        for name in BODY_HEAD_NAMES:
            measured = body[name]
            if (isinstance(measured, bool) or not isinstance(measured, (int, float))
                    or not math.isfinite(measured)
                    or (not compensated and abs(measured-calibration['body_head_reference_deg'][name]) > scene['reference_tolerance_deg'])):
                raise ValueError('boot_recheck_joint_window_reference_changed:' + name)
    if (samples[-1]['received_ns'] - samples[0]['received_ns'] < 300_000_000
            or observed-samples[-1]['received_ns'] > 500_000_000
            or _canonical(samples[-1]['body_head_deg']) != _canonical(raw['body_head_deg'])):
        raise ValueError('boot_recheck_joint_window_incomplete_or_stale')


def build_boot_receipt(raw, evidence_path, *, calibration, calibration_sha256,
                       scene, task_hash, current_boot_id, now_ns):
    """Validate observed data and bind the stored raw bytes; does not write or authorize."""
    if not isinstance(raw, dict) or raw.get('schema') != CAPTURE_SCHEMA:
        raise ValueError('boot_recheck_capture_schema_required')
    if not isinstance(task_hash, str) or len(task_hash) != 64:
        raise ValueError('boot_recheck_task_hash_required')
    checks = _check_measurements(raw, calibration=calibration, calibration_sha256=calibration_sha256,
        scene=scene, current_boot_id=current_boot_id, now_ns=now_ns)
    _check_capture_window(raw, calibration=calibration, scene=scene)
    path = Path(evidence_path).resolve()
    stored, sha = _file(evidence_path)
    if _canonical(stored) != _canonical(raw):
        raise ValueError('boot_recheck_raw_evidence_changed')
    return dict(schema=SCHEMA, task_hash=task_hash,
        **{key: raw[key] for key in MEASUREMENT_KEYS}, checks=checks,
        evidence_files=[dict(kind='boot_capture', path=str(path), sha256=sha)])


def validate_boot_recheck(receipt, *, base, calibration, calibration_sha256,
                          scene, task_hash, current_boot_id, now_ns):
    """Check a stored receipt against its original evidence and the active frozen task."""
    if (not isinstance(receipt, dict) or receipt.get('schema') != SCHEMA
            or receipt.get('task_hash') != task_hash):
        raise ValueError('boot_recheck_schema_or_task_binding_mismatch')
    checks = _check_measurements(receipt, calibration=calibration, calibration_sha256=calibration_sha256,
        scene=scene, current_boot_id=current_boot_id, now_ns=now_ns)
    evidence = receipt.get('evidence_files')
    if (not isinstance(evidence, list) or len(evidence) != 1 or not isinstance(evidence[0], dict)
            or evidence[0].get('kind') != 'boot_capture' or not isinstance(evidence[0].get('path'), str)):
        raise ValueError('boot_recheck_raw_evidence_required')
    reference = evidence[0]
    path = Path(reference['path'])
    if not path.is_absolute():
        path = Path(base) / path
    raw, sha = _file(path)
    if (sha != reference.get('sha256') or raw.get('schema') != CAPTURE_SCHEMA
            or any(_canonical(raw.get(key)) != _canonical(receipt.get(key)) for key in MEASUREMENT_KEYS)
            or _canonical(checks) != _canonical(receipt.get('checks'))):
        raise ValueError('boot_recheck_raw_evidence_binding_mismatch')
    _check_capture_window(raw, calibration=calibration, scene=scene)
    return dict(schema=SCHEMA, current_boot_id=current_boot_id,
                calibration_capture_boot_id=calibration['boot_id'], task_hash=task_hash,
                evidence_sha256=sha, **checks)

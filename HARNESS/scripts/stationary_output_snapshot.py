"""JSON handoff of a committed stationary F1 output, for planning experiments.

Call on the owner thread between completed samples. This does not wait for rest,
reset a moving planner, authorize motion, or time-rebase a delayed result. An
unsupported state means the optional predictor cannot use this representation.
Measured feedback is retained separately from exact commanded native state.
"""
from copy import deepcopy
import json
import math
import numpy as np

from f1_harness.contracts import digest
from output_branch import fork_output

SCHEMA = 'f1-stationary-planning-snapshot-v1'
PROFILE_FIELDS = ('size', 'dt', 'max_velocity', 'max_acceleration', 'max_jerk',
                  'target_cutoff_hz', 'deadband')
OUTPUT_FIELDS = ('position', 'target', 'updated', 'sampled', 'timed_out', 'fault',
                 '_recoverable_timing_fault', '_neutral_since', '_neutral_packets', 'enabled')
NATIVE_FIELDS = ('position', 'velocity', 'acceleration', 'filtered_target',
                 'initialized', 'recovery_events')
CONTEXT_FIELDS = ('owner_instance_id', 'session_id', 'task_hash', 'epoch', 'request_id',
                  'calibration_id', 'source_plan_sha256', 'control_profile_sha256',
                  'numerical_asset_id', 'control_source_sha256')


class UnsupportedSnapshot(ValueError):
    """Optional planning representation unavailable; not a motion interlock."""


class _TerminalCertificate:
    """Planning-only view of an already elapsed curve's exact terminal sample.

    F1's fallback evaluates min(elapsed, duration), with elapsed only increasing.
    No earlier part of the curve is invented or exposed. New proposals still
    construct and validate their own native Ruckig trajectories normally.
    """
    __slots__ = ('_duration', '_endpoint')

    def __init__(self, duration, endpoint):
        self._duration, self._endpoint = duration, deepcopy(endpoint)

    @property
    def duration(self):
        return self._duration

    def at_time(self, value):
        if not math.isfinite(value) or value < self.duration:
            raise UnsupportedSnapshot('terminal_certificate_has_no_earlier_curve')
        return deepcopy(self._endpoint)


def _json_copy(value):
    def convert(obj):
        if isinstance(obj, np.ndarray): return obj.tolist()
        if isinstance(obj, np.generic): return obj.item()
        raise TypeError(type(obj).__name__)
    raw = json.dumps(value, allow_nan=False, default=convert)
    if len(raw.encode()) > 65536: raise UnsupportedSnapshot('snapshot_exceeds_ipc_frame')
    return json.loads(raw)


def _profiles(output):
    planner = output.planner
    return _json_copy(dict(output=dict(max_velocity=output.max_velocity, timeout=output.timeout),
        adapter={k: getattr(planner, k) for k in PROFILE_FIELDS},
        native={k: getattr(planner._radians, k) for k in (*PROFILE_FIELDS, 'alpha')},
        bounds_rad=planner.bounds))


def _vector(value, size):
    if not isinstance(value, list) or len(value) != size or any(
            type(v) not in (int, float) or not math.isfinite(v) for v in value):
        raise UnsupportedSnapshot('invalid_snapshot_vector')


def _validate(payload):
    if (payload.get('schema') != SCHEMA or payload.get('planning_only') is not True
            or payload.get('execution_authorized') is not False):
        raise UnsupportedSnapshot('snapshot_scope_mismatch')
    context = payload['context']
    if not isinstance(context, dict) or set(CONTEXT_FIELDS) - context.keys():
        raise UnsupportedSnapshot('snapshot_identity_incomplete')
    if type(context['epoch']) is not int or context['epoch'] < 0:
        raise UnsupportedSnapshot('snapshot_epoch_invalid')
    for key in CONTEXT_FIELDS:
        if key != 'epoch' and (not isinstance(context[key], str) or not context[key]):
            raise UnsupportedSnapshot('snapshot_identity_incomplete')
    state, native = payload['output'], payload['native']
    if set(state) != set(OUTPUT_FIELDS) or set(native) != set(NATIVE_FIELDS):
        raise UnsupportedSnapshot('snapshot_state_fields_mismatch')
    for key in ('position', 'target'): _vector(state[key], 14)
    for key in NATIVE_FIELDS[:4]: _vector(native[key], 14)
    _vector(payload['position_degrees'], 14)
    if state['position'] != payload['position_degrees']:
        raise UnsupportedSnapshot('snapshot_degree_state_inconsistent')
    for key in ('updated', 'sampled'):
        if type(state[key]) not in (int, float) or not math.isfinite(state[key]):
            raise UnsupportedSnapshot('invalid_snapshot_time')
    if (state['updated'] > state['sampled'] or state['fault'] is not None
            or state['timed_out'] is not False or state['_recoverable_timing_fault'] is not False
            or state['_neutral_since'] is not None or state['_neutral_packets'] != 0):
        raise UnsupportedSnapshot('snapshot_not_healthy_committed_state')
    if native['initialized'] is not True or type(native['recovery_events']) is not int or native['recovery_events'] < 0:
        raise UnsupportedSnapshot('snapshot_native_state_invalid')
    if (any(v != 0. for k in ('velocity', 'acceleration') for v in native[k])
            or native['filtered_target'] != native['position']):
        raise UnsupportedSnapshot('snapshot_requires_exact_stationary_command_state')
    if (not isinstance(state['enabled'], list) or len(state['enabled']) != 2
            or any(type(v) is not bool for v in state['enabled'])
            or payload['enabled_axes'] != [v for v in state['enabled'] for _ in range(7)]):
        raise UnsupportedSnapshot('snapshot_enable_state_mismatch')
    feedback = payload['feedback']
    _vector(feedback['measured16'], 16)
    if type(feedback['measured_at_ns']) is not int or feedback['measured_at_ns'] < 0:
        raise UnsupportedSnapshot('invalid_feedback_time')
    cert = payload['certificate']
    if cert is not None:
        if (any(type(cert[k]) not in (int, float) or not math.isfinite(cert[k]) for k in ('duration_s', 'elapsed_s'))
                or not 0. <= cert['duration_s'] <= cert['elapsed_s']):
            raise UnsupportedSnapshot('moving_certificate_cannot_use_terminal_snapshot')
        if not isinstance(cert['endpoint'], list) or len(cert['endpoint']) != 3:
            raise UnsupportedSnapshot('invalid_certificate_endpoint')
        for vector in cert['endpoint']: _vector(vector, 14)


def capture(output, *, context, measured16, measured_at_ns):
    """Copy exact internal state plus separate measurement; never mutate source."""
    # Reuse the existing concrete-type/committed-sample checks. Capture is on
    # the same thread as publication; this is not a concurrent deep-copy API.
    _, before = fork_output(output)
    planner = output.planner
    cert = planner._certificate
    if cert is not None and cert[1] < cert[0].duration:
        raise UnsupportedSnapshot('moving_certificate_cannot_use_terminal_snapshot')
    payload = _json_copy(dict(schema=SCHEMA, planning_only=True, execution_authorized=False,
        context=context, profiles=_profiles(output),
        output={k: getattr(output, k) for k in OUTPUT_FIELDS},
        native={k: getattr(planner._radians, k) for k in NATIVE_FIELDS},
        position_degrees=planner._position_degrees, enabled_axes=planner._enabled,
        limit_diagnostics=planner.limit_diagnostics,
        certificate=None if cert is None else dict(duration_s=cert[0].duration,
            elapsed_s=cert[1], endpoint=cert[0].at_time(cert[0].duration)),
        feedback=dict(measured16=measured16, measured_at_ns=measured_at_ns)))
    _validate(payload)
    if before != fork_output(output)[1]: raise UnsupportedSnapshot('source_changed_during_capture')
    return dict(payload=payload, sha256=digest(payload))


def restore(snapshot, *, factory, expected_context):
    """Construct a planning-only output in a fresh process with matching profile.

    The factory is trusted local code, never named or imported by the message.
    This preserves original clocks; calling it later does not refresh freshness.
    It does not restore an owner, transport, authority, or physical feedback.
    """
    snapshot = _json_copy(snapshot)
    payload = snapshot['payload']
    if digest(payload) != snapshot['sha256']: raise UnsupportedSnapshot('snapshot_digest_mismatch')
    _validate(payload)
    if payload['context'] != expected_context: raise UnsupportedSnapshot('snapshot_context_mismatch')
    output = factory()
    from f1dagger.arm_output import RadianArmTrajectory
    from pico_f1.arm_output import ApprovedArmOutput
    if type(output) is not ApprovedArmOutput or type(output.planner) is not RadianArmTrajectory:
        raise UnsupportedSnapshot('unsupported_output_implementation')
    if output.position is not None or _profiles(output) != payload['profiles']:
        raise UnsupportedSnapshot('snapshot_factory_not_fresh_or_profile_mismatch')
    for key in OUTPUT_FIELDS:
        value = deepcopy(payload['output'][key])
        setattr(output, key, np.asarray(value, dtype=float) if key in ('position', 'target') else value)
    planner = output.planner
    for key in NATIVE_FIELDS: setattr(planner._radians, key, deepcopy(payload['native'][key]))
    planner._position_degrees = payload['position_degrees'].copy()
    planner._enabled = np.asarray(payload['enabled_axes'], dtype=bool)
    planner.limit_diagnostics = deepcopy(payload['limit_diagnostics'])
    planner._pending = planner._next_certificate = None
    cert = payload['certificate']
    planner._certificate = None if cert is None else (
        _TerminalCertificate(cert['duration_s'], cert['endpoint']), cert['elapsed_s'])
    if capture(output, context=expected_context, **payload['feedback']) != snapshot:
        raise UnsupportedSnapshot('snapshot_roundtrip_changed_state')
    return output

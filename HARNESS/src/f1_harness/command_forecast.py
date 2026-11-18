"""Command-path prediction with independent sensor evidence, without robot I/O.

The predictor is trusted local numerical code, not model-provided code. A
successful forecast proves neither physical following nor permission to move.
Publication receipts must come from the same owner as the supplied smoother;
these structural checks are not authentication or an asynchronous acceptance.
"""
from copy import deepcopy

import numpy as np

from .contracts import digest


def _vector(value):
    result = np.asarray(value, dtype=float)
    if result.shape != (16,) or not np.isfinite(result).all():
        raise ValueError('forecast_requires_finite_vector16')
    return result.copy()


def _stamp(value):
    if type(value) is not int or value < 0:
        raise ValueError('forecast_requires_nonnegative_integer_timestamp')
    return value


def forecast_commands(output, *, publication, feedback, predictor, **plan):
    """Forecast from a complete committed command; retain feedback unchanged.

    sample_ns and feedback stamps share an explicitly named sampling clock.
    Owner receipt times are deliberately NOT compared to that clock: native
    publication uses process monotonic time even in a virtual-time replay.
    Field publication timestamps can predate this tick for unchanged grippers.
    """
    publication = deepcopy(publication)
    feedback = deepcopy(feedback)
    record = publication['record']
    domain = publication.get('sample_clock')
    if (not isinstance(domain, str) or not domain
            or feedback.get('sample_clock') != domain):
        raise ValueError('forecast_sampling_clock_mismatch')
    if (record.get('publication_complete') is not True
            or record.get('stage') != 'completed'
            or type(record.get('command_sequence')) is not int
            or record['command_sequence'] < 1):
        raise ValueError('forecast_requires_complete_publication')
    command = _vector(record['attempted_command'])
    measured = _vector(feedback['measured16'])
    sample_ns = _stamp(publication['sample_ns'])
    received_ns = _stamp(feedback['received_ns'])
    stamps = feedback['source_stamps_ns']
    if not isinstance(stamps, (list, tuple)) or len(stamps) != 16:
        raise ValueError('forecast_feedback_timestamps_required')
    for stamp in stamps:
        if _stamp(stamp) > received_ns:
            raise ValueError('forecast_feedback_after_receipt')
    if 'measured16' in plan:
        raise ValueError('forecast_ambiguous_legacy_measurement')
    # Legacy numerical API uses this vector as a command seed, not as sensor
    # input. It independently checks arm positions against the committed state.
    result = predictor(output, measured16=command, **plan)
    if round(result['input_state']['sample_time_s'] * 1e9) != sample_ns:
        raise ValueError('forecast_publication_sample_mismatch')
    rows = []
    previous = command
    for old in result['rows']:
        row = dict(old)
        if not np.array_equal(_vector(row.pop('measured16')), previous):
            raise ValueError('forecast_previous_command_discontinuous')
        row.update(previous_submitted16=previous.tolist(), evidence_kind='predicted_command')
        previous = _vector(row['submitted16'])
        rows.append(row)
    result.update(schema='f1-command-segment-forecast-v1', rows=rows,
        publication=publication, feedback=feedback,
        input_evidence_sha256=digest(dict(publication=publication, feedback=feedback)),
        initial_command_minus_feedback16=(command-measured).tolist(),
        physical_feedback_predicted=False, requires_current_physical_revalidation=True,
        execution_authorized=False, audit_scope='command_path_only')
    result['limitations'] = [
        'Conditional command path only; sensor feedback is retained, not extrapolated.',
        'Declared schedule; no prediction of contact, following error, arrival or success.',
        'No online authority, asynchronous result acceptance or transport is implemented here.',
    ]
    return result


def sweep_anchor(rows):
    """Select a declared command anchor or a legacy recorded feedback anchor.

    Canonical forecasts cannot silently fall back to measurements, nor mix
    command predictions with physical/ideal-following replay rows.
    """
    if not rows:
        raise ValueError('empty_sweep_is_not_evidence')
    predicted = rows[0].get('evidence_kind') == 'predicted_command'
    previous = None
    for row in rows:
        if (row.get('evidence_kind') == 'predicted_command') != predicted:
            raise ValueError('mixed_sweep_evidence')
        if predicted:
            if 'measured16' in row:
                raise ValueError('prediction_cannot_claim_measurement')
            anchor = _vector(row['previous_submitted16'])
            if previous is not None and not np.array_equal(anchor, previous):
                raise ValueError('forecast_previous_command_discontinuous')
            previous = _vector(row['submitted16'])
    return _vector(rows[0]['previous_submitted16'] if predicted else rows[0]['measured16'])

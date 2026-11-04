"""Separate the controller input from its smoothed physical publication.

Both are retained. Actor/Q actions must use the same side of the controller;
changing representation requires an explicit recipe/cache/critic revision.
"""
import math

SUBMITTED = 'submitted_relative_eef10_v1'
REFERENCE = 'approved_reference_relative_eef10_v1'
DECISION = 'decision_prefix_relative_eef10_v1'


def action_joints(record, contract=SUBMITTED):
    field = {SUBMITTED: 'submitted16', REFERENCE: 'selected16'}.get(contract)
    if field is None:
        raise ValueError('Unknown RLT action representation')
    values = record.get(field)
    mask = record['action_time_mask']
    if (not isinstance(values, list) or len(values) != sum(mask)
            or any(len(v) != 16 or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in v)
                   for v in values)):
        raise ValueError('Missing verified controller action: '+field)
    return values

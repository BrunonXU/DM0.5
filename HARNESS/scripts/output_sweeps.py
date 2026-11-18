"""Shared offline task-scene audit for reference-preview or simulated outputs.

Only transfer and empty return are covered. Contact stages require their own
contracts; an empty result is not proof of a complete cycle.
"""
import numpy as np

from f1_harness.contracts import Observation
from f1_harness.geometry import transform
from f1_harness.skills import _motion_clear
from f1_harness.command_forecast import sweep_anchor


def sweep_outputs(outputs, events, service, numeric):
    sweeps = []
    for event in events:
        if event['kind'] != 'harness_planning_started':
            continue
        intent = event['intent']
        if intent['skill'] not in ('transfer_above_hole', 'withdraw_reset'):
            continue
        rows = [row for row in outputs if row['request_id'] == intent['request_id']]
        if not rows:
            continue
        full = event['full_reference']
        scene = service._scene(Observation.model_validate(event['observation']), full,
                               event['held_reference'], skill=intent['skill'])
        before = sweep_anchor(rows)
        previous = transform(numeric.dispatch('forward_pose', dict(full22=full, held16=before.tolist())))
        failures = []
        count = 0
        for row in rows:
            target = np.array(row['submitted16'])
            steps = max(1, int(np.ceil(np.max(abs(target[:14] - before[:14])) / .1)))
            for fraction in np.linspace(0, 1, steps + 1)[1:]:
                value = before + fraction * (target - before)
                numeric.numerics.mapping.check_action(value)
                numeric.numerics.gate.check_preventive(value, full)
                current = transform(numeric.dispatch('forward_pose', dict(full22=full, held16=value.tolist())))
                if not _motion_clear(scene, previous, current, scene.eef_object):
                    failures.append(dict(virtual_ns=row['virtual_ns'], fraction=float(fraction)))
                previous = current
                count += 1
            before = target
        sweeps.append(dict(skill=intent['skill'], request_id=intent['request_id'], samples=count,
                           evidence_scope='command_path_only' if rows[0].get('evidence_kind')=='predicted_command' else 'recorded_output_path',
                           max_joint_step_deg=.1, failures=failures, passed=not failures))
    return sweeps

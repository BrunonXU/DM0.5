"""Offline dense sweep partitioning; no robot transport or execution authority.

The caller owns worker initialization and ordered execution. Each block carries
the exact previous *checked* configuration, including interpolation roundoff,
so splitting a path does not remove motion checks across block boundaries.
"""
import numpy as np

from f1_harness.geometry import transform
from f1_harness.skills import _motion_clear
from f1_harness.command_forecast import sweep_anchor


def make_blocks(rows, full, scene, *, block_samples=64):
    if type(block_samples) is not int or block_samples < 1:
        raise ValueError('positive_integer_block_samples_required')
    if not rows:
        raise ValueError('empty_sweep_is_not_evidence')

    def vector(value):
        result = np.asarray(value, dtype=float)
        if result.shape != (16,) or not np.isfinite(result).all():
            raise ValueError('finite_sweep_vector16_required')
        return result

    before = sweep_anchor(rows)
    anchor = before.tolist()
    pending, jobs = [], []
    for row in rows:
        target = vector(row['submitted16'])
        steps = max(1, int(np.ceil(np.max(abs(target[:14] - before[:14])) / .1)))
        for fraction in np.linspace(0, 1, steps + 1)[1:]:
            checked = (before + fraction * (target - before)).tolist()
            pending.append((checked, row['virtual_ns'], float(fraction)))
            if len(pending) == block_samples:
                jobs.append((full, scene, anchor, pending))
                pending, anchor = [], checked
        # The original serial loop uses the raw target for next interpolation,
        # but its previous FK pose remains the last checked interpolated value.
        before = target
    if pending:
        jobs.append((full, scene, anchor, pending))
    return jobs


def check_block(numeric, job, *, stop_on_failure=False):
    full, scene, anchor, points = job
    if not points:
        raise ValueError('empty_sweep_block')
    previous = transform(numeric.dispatch('forward_pose', dict(full22=full, held16=anchor)))
    failures = []
    for count, (value, stamp, fraction) in enumerate(points, 1):
        numeric.numerics.mapping.check_action(value)
        numeric.numerics.gate.check_preventive(value, full)
        current = transform(numeric.dispatch('forward_pose', dict(full22=full, held16=value)))
        if not _motion_clear(scene, previous, current, scene.eef_object):
            failures.append(dict(virtual_ns=stamp, fraction=fraction))
            if stop_on_failure:
                return dict(samples=count, failures=failures, stopped_on_first_failure=True)
        previous = current
    return dict(samples=len(points), failures=failures)


def combine_blocks(jobs, ordered_results):
    """All jobs must complete; exceptions/partial worker results are not success.

    Results must come from ordered map, never from unordered completion order.
    The ordinal is checked as well as counts because same-size jobs can swap.
    """
    values = list(ordered_results)
    if not jobs or len(values) != len(jobs):
        raise ValueError('incomplete_sweep_results')
    total, failures = 0, []
    for index, (job, value) in enumerate(zip(jobs, values)):
        if value['block_index'] != index or value['samples'] != len(job[3]):
            raise ValueError('sweep_result_identity_or_count_mismatch')
        total += value['samples']
        failures.extend(value['failures'])
    return dict(samples=total, failures=failures)

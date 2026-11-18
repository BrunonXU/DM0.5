"""Partitioning must retain every original check, including boundary segments."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

spec = importlib.util.spec_from_file_location('sweep_blocks',
    Path(__file__).parents[1] / 'scripts/sweep_blocks.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture():
    rng = np.random.default_rng(729)
    before = rng.uniform(-1, 1, 16)
    rows = [dict(measured16=before.tolist(), submitted16=rng.uniform(-1, 1, 16).tolist(),
                 virtual_ns=i) for i in range(7)]
    rows[0]['measured16'][7] = 1.
    rows[0]['submitted16'][7] = .1
    return rows, [0.] * 22, SimpleNamespace(eef_object=np.eye(4))


def oracle(rows):
    before = np.array(rows[0]['measured16'])
    previous = before.tolist()
    pairs, samples = [], []
    for row in rows:
        target = np.array(row['submitted16'])
        count = max(1, int(np.ceil(np.max(abs(target[:14] - before[:14])) / .1)))
        for fraction in np.linspace(0, 1, count + 1)[1:]:
            checked = (before + fraction * (target - before)).tolist()
            pairs.append((previous, checked))
            samples.append((checked, row['virtual_ns'], float(fraction)))
            previous = checked
        before = target
    return pairs, samples


@pytest.mark.parametrize('size', [1, 2, 7, 64, 10000])
def test_partitions_preserve_every_query_and_inter_block_motion(size):
    rows, full, scene = fixture()
    pairs, samples = oracle(rows)
    jobs = module.make_blocks(rows, full, scene, block_samples=size)
    actual_pairs, actual_samples = [], []
    for _, _, previous, points in jobs:
        for checked, stamp, fraction in points:
            actual_pairs.append((previous, checked))
            actual_samples.append((checked, stamp, fraction))
            previous = checked
    assert actual_samples == samples
    assert actual_pairs == pairs
    # Ensure this fixture actually exercises raw target / interpolated mismatch.
    assert any(point != rows[stamp]['submitted16'] for point, stamp, f in samples if f == 1.)


def test_original_native_and_scene_checks_are_kept(monkeypatch):
    rows, full, scene = fixture()
    jobs = module.make_blocks(rows, full, scene, block_samples=7)
    pairs, samples = oracle(rows)
    actions, gates, segments = [], [], []
    def pose(method, arguments):
        assert method == 'forward_pose' and arguments['full22'] == full
        value = np.eye(4); value[:3, 3] = arguments['held16'][7:10]
        return value
    numeric = SimpleNamespace(dispatch=pose, numerics=SimpleNamespace(
        mapping=SimpleNamespace(check_action=lambda q: actions.append(q)),
        gate=SimpleNamespace(check_preventive=lambda q, ref: gates.append((q, ref)))))
    def clear(_scene, before, after, held):
        assert _scene is scene and held is scene.eef_object
        segments.append((before[:3, 3].tolist(), after[:3, 3].tolist()))
        return after[0, 3] <= .25
    monkeypatch.setattr(module, '_motion_clear', clear)
    results = [dict(module.check_block(numeric, job), block_index=i) for i, job in enumerate(jobs)]
    merged = module.combine_blocks(jobs, results)
    assert actions == [s[0] for s in samples]
    assert gates == [(s[0], full) for s in samples]
    assert segments == [(a[7:10], b[7:10]) for a, b in pairs]
    assert merged == dict(samples=len(samples), failures=[
        dict(virtual_ns=stamp, fraction=f) for q, stamp, f in samples if q[7] > .25])


def test_native_error_propagates_and_partial_results_are_not_success():
    rows, full, scene = fixture()
    jobs = module.make_blocks(rows, full, scene)
    def failed(*args, **kwargs):
        raise RuntimeError('native_failure')
    with pytest.raises(RuntimeError, match='native_failure'):
        module.check_block(SimpleNamespace(dispatch=failed), jobs[0])
    with pytest.raises(ValueError, match='incomplete'):
        module.combine_blocks(jobs, [])
    def interrupted_results():
        yield dict(block_index=0, samples=len(jobs[0][3]), failures=[])
        raise RuntimeError('worker_exited')
    with pytest.raises(RuntimeError, match='worker_exited'):
        module.combine_blocks(jobs, interrupted_results())


def test_unordered_or_missing_checks_cannot_be_merged():
    rows, full, scene = fixture()
    jobs = module.make_blocks(rows, full, scene, block_samples=7)
    values = [dict(block_index=i, samples=len(job[3]), failures=[]) for i, job in enumerate(jobs)]
    values[0], values[1] = values[1], values[0]
    with pytest.raises(ValueError, match='identity_or_count'):
        module.combine_blocks(jobs, values)
    values[0], values[1] = values[1], values[0]
    values[0]['samples'] -= 1
    with pytest.raises(ValueError, match='identity_or_count'):
        module.combine_blocks(jobs, values)


def test_bad_inputs_do_not_create_vacuous_passes():
    rows, full, scene = fixture()
    for size in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match='positive_integer'):
            module.make_blocks(rows, full, scene, block_samples=size)
    with pytest.raises(ValueError, match='empty_sweep'):
        module.make_blocks([], full, scene)
    rows[0]['submitted16'][3] = float('nan')
    with pytest.raises(ValueError, match='finite_sweep'):
        module.make_blocks(rows, full, scene)


def test_first_counterexample_stops_only_the_rejected_block(monkeypatch):
    rows, full, scene = fixture()
    job = module.make_blocks(rows, full, scene, block_samples=64)[0]
    seen = []
    def pose(method, arguments):
        return np.eye(4)
    numeric = SimpleNamespace(dispatch=pose, numerics=SimpleNamespace(
        mapping=SimpleNamespace(check_action=lambda q: seen.append(q)),
        gate=SimpleNamespace(check_preventive=lambda q, ref: None)))
    monkeypatch.setattr(module, '_motion_clear', lambda *args: False)
    rejected = module.check_block(numeric, job, stop_on_failure=True)
    assert rejected['samples'] == len(seen) == 1
    assert rejected['stopped_on_first_failure']
    assert rejected['failures'] == [dict(virtual_ns=job[3][0][1], fraction=job[3][0][2])]
    seen.clear()
    monkeypatch.setattr(module, '_motion_clear', lambda *args: True)
    clear = module.check_block(numeric, job, stop_on_failure=True)
    assert clear == dict(samples=len(job[3]), failures=[])
    assert len(seen) == len(job[3])

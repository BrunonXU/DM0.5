"""Unit tests for H-43: configurable IK max_wall_s and align endpoint-IK variant ordering."""
from types import SimpleNamespace
import time

import numpy as np
import pytest

from f1_harness.transport import rank_alignment_feeds


def _align_scene():
    hole = np.eye(4)
    hole[:3, 3] = [0.70, -0.04, 0.77]
    eef = np.eye(4)
    eef[:3, 3] = [0.70, -0.04, 0.90]
    attach = np.eye(4)
    attach[:3, 3] = [0.0, 0.0, -0.06]
    return SimpleNamespace(
        object_held=True,
        eef_object=attach,
        base_eef=eef,
        base_hole=hole,
        aperture_measured=True,
        object_insertion_reference_height_m=0.0,
        transfer_clearance_m=0.04,
        insertion_depth_m=0.012,
        attached_radius_m=0.02,
        tool_local_boxes=None,
        eef_tip_reference=None,
        object_local_box=dict(min=[-0.015, -0.015, 0.0], max=[0.015, 0.015, 0.06]),
        admission_profile='development_coarse',
        object_planning_margin_m=0.001,
        planning_margin_m=0.015,
        uncertainty_m=0.002,
        obstacles=(),
        tool_padding_policy='required',
        target_object_id='square-1',
    )


def test_h43_harness_numerics_default_7s_and_cap_16s_and_caller_budget(monkeypatch):
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    candidates = [
        root / 'control/src',
        root.parent / 'DAgger/F1/src',
        root.parent / 'DAgger/shared/src',
        Path('/home/xza_100850113/projects/DAgger/F1/src'),
        Path('/home/xza_100850113/projects/DAgger/shared/src'),
    ]
    for path in candidates:
        if path.is_dir() and str(path) not in sys.path:
            sys.path.insert(0, str(path))
    import f1dagger.harness_compute as hc

    dummy_gate = SimpleNamespace(mapping=SimpleNamespace())
    monkeypatch.setattr(hc, 'collision_gate_from_spec', lambda cfg: dummy_gate)
    monkeypatch.setattr(hc, 'F1ArmTarget', lambda urdf, mapping, ik: SimpleNamespace())

    base_spec = dict(
        gate={},
        urdf='dummy.urdf',
        ik={'safety': {}},
        position_tolerance_m=0.0005,
        rotation_tolerance_rad=0.005,
        max_iterations=3000,
    )
    # Default is 7.0 s
    n_default = hc.HarnessNumerics(base_spec)
    assert n_default.max_wall_s == 7.0

    # Valid site value 15.0 s
    spec_15 = dict(base_spec, ik={'safety': {}, 'max_wall_s': 15.0})
    n_15 = hc.HarnessNumerics(spec_15)
    assert n_15.max_wall_s == 15.0

    # Cap 16.0 s allowed; > 16.0 s rejected; <= 0 / bool / NaN rejected
    assert hc.HarnessNumerics(dict(base_spec, ik={'safety': {}, 'max_wall_s': 16.0})).max_wall_s == 16.0
    for bad in (16.1, 20.0, 0.0, -1.0, True, float('nan'), float('inf'), '15'):
        with pytest.raises(ValueError, match='invalid_ik_max_wall_s'):
            hc.HarnessNumerics(dict(base_spec, ik={'safety': {}, 'max_wall_s': bad}))

    # Caller remaining budget still clamps below max_wall_s
    t0 = 100.0
    assert hc.planning_deadline({'planning_budget_s': 4.5}, n_15.max_wall_s, 'err', started=t0) == pytest.approx(104.5)
    assert hc.planning_deadline({'planning_budget_s': 25.0}, n_15.max_wall_s, 'err', started=t0) == pytest.approx(115.0)
    assert hc.planning_deadline({}, n_default.max_wall_s, 'err', started=t0) == pytest.approx(107.0)


def test_h43_align_ranking_orders_by_margin_keeps_all_and_puts_failed_last():
    scene = _align_scene()
    variants = [0, 1, 2, 3]
    ranking = [dict(variant=v) for v in variants]

    class _FakeNumerics:
        def call(self, method, **kwargs):
            assert method == 'endpoint_ik'
            assert len(kwargs['goals']) == 4
            # v=0: failed; v=1: margin 28.5; v=2: margin 3.2; v=3: failed
            return dict(
                results=[
                    dict(converged=False, min_margin_deg=None, final_arm_deg=None, elapsed_ms=4.0),
                    dict(converged=True, min_margin_deg=28.5, final_arm_deg=[0]*7, elapsed_ms=3.5),
                    dict(converged=True, min_margin_deg=3.2, final_arm_deg=[0]*7, elapsed_ms=4.2),
                    dict(converged=False, min_margin_deg=None, final_arm_deg=None, elapsed_ms=5.1),
                ],
                elapsed_ms=16.8,
            )

    ordered, rows = rank_alignment_feeds(
        scene, variants, ranking,
        numerics=_FakeNumerics(),
        full22=[0.0]*22,
        held16=[0.0]*16,
    )
    assert ordered == [1, 2, 0, 3]
    assert set(ordered) == {0, 1, 2, 3}
    assert len(rows) == 4
    assert rows[0]['variant'] == 1 and rows[0]['endpoint_ik']['min_margin_deg'] == 28.5
    assert rows[-1]['endpoint_ik']['converged'] is False
    assert rows[-2]['endpoint_ik']['converged'] is False
    assert all('endpoint_ik_ranking_ms' in r for r in rows)


def test_h43_slow_ranking_logs_warning_without_blocking(caplog):
    scene = _align_scene()

    class _SlowNumerics:
        def call(self, method, **kwargs):
            time.sleep(0.01)
            return dict(
                results=[dict(converged=True, min_margin_deg=float(i), final_arm_deg=[0]*7, elapsed_ms=1.0)
                         for i in range(4)],
                elapsed_ms=1.0,
            )

    # Monkeypatch perf_counter in transport via timing threshold check
    import time as _t
    orig = _t.perf_counter
    calls = iter([10.0, 11.25])
    _t.perf_counter = lambda: next(calls)
    try:
        ordered, rows = rank_alignment_feeds(
            scene, [0, 1, 2, 3], [dict(variant=v) for v in range(4)],
            numerics=_SlowNumerics(), full22=[0.0]*22, held16=[0.0]*16,
        )
    finally:
        _t.perf_counter = orig
    assert ordered == [3, 2, 1, 0]
    assert rows[0].get('endpoint_ik_ranking_slow') is True
    assert rows[0]['endpoint_ik_ranking_ms'] >= 1000.0

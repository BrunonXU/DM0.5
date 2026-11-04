"""Numerical RTC and wire validation, including core/paper time direction."""
import numpy as np
import pytest
import torch

from f1_toys.rtc import guided_velocity, soft_mask, reanchor, absolute_eef, decode_window
from f1_toys.eef import solver, normalize_actions, encode_actions
from f1_toys.server import validate_rtc


def test_vjp_matches_analytic_nondiagonal_jacobian_and_sign():
    x = torch.tensor([.2, -.4], dtype=torch.float64, requires_grad=True)
    matrix = torch.tensor([[.4, -.2], [.7, .1]], dtype=torch.float64)
    target = torch.tensor([.9, .1], dtype=torch.float64)
    weight = torch.tensor([1., .3], dtype=torch.float64)
    t = .6
    v = matrix @ x
    jacobian = torch.eye(2, dtype=x.dtype) - t*matrix
    expected = v - min(5., ((1-t)**2+t*t)/((1-t)*t)) * jacobian.T @ ((target-(x-t*v))*weight)
    torch.testing.assert_close(guided_velocity(x, lambda z: matrix@z, t, target, weight), expected)


def test_mask_equation_and_invalid_geometry():
    mask = soft_mask(50, 12, 20)
    np.testing.assert_array_equal(mask[:12], 1)
    np.testing.assert_array_equal(mask[30:], 0)
    c = 18/19
    assert mask[12] == pytest.approx(c*np.expm1(c)/np.expm1(1))
    assert np.all(np.diff(mask) <= 0)
    with pytest.raises(ValueError): soft_mask(50, 26, 26)


def test_reanchor_roundtrip_committed_targets_and_unused_dimensions():
    ik = solver()
    state = np.array([0.]*7+[-70.,-90.,90.,-85.,0.,0.,0.]+[0.,30.])
    actions = np.repeat(state[None], 50, axis=0)
    actions[:, 8] += np.linspace(0, 1, 50)
    stats = dict(mean=[.01, .02, .03], std=[.1, .2, .3])
    raw = normalize_actions(encode_actions(ik.model, state, actions), stats)
    absolute = absolute_eef(ik, state, raw, stats)
    new_state = state.copy(); new_state[8] += .3
    target = reanchor(ik, new_state, absolute[20:], actions[20:32], stats)[0]
    expected = normalize_actions(encode_actions(ik.model, new_state, actions[20:]), stats)
    np.testing.assert_allclose(target[:30], expected, atol=1e-6)
    np.testing.assert_array_equal(target[:,10:], 0)


def test_decode_never_resolves_committed_joints():
    class FakeIK:
        model = type('M', (), {'fk': lambda *args: np.eye(4)})()
        def solve(self, side, pose, reference):
            np.testing.assert_array_equal(reference, np.arange(7)+7)
            return dict(success=True, joints=reference.tolist())
    committed = np.arange(16, dtype=float)[None]
    physical = np.array([[0,0,0,1,0,0,0,1,0,0]]*3, float)
    result = decode_window(FakeIK(), physical, np.zeros(16), np.zeros(16), committed, 2)
    np.testing.assert_array_equal(result['actions'][0], committed[0])
    assert len(result['diagnostics']) == 1


def test_wire_rejects_wrong_shape_timing_and_base_identity():
    context = dict(bootstrap=False, delay_steps=12, shift_steps=20, coverage_steps=34,
        plan_origin_ns=100, base_plan_id=1, committed_actions16=[[0.]*16]*12,
        previous_absolute_eef10=[[0.]*10]*30)
    validate_rtc(context, 2)
    for patch in (dict(delay_steps=26), dict(base_plan_id=2), dict(coverage_steps=31),
                  dict(committed_actions16=[]), dict(bootstrap=True)):
        with pytest.raises(ValueError): validate_rtc({**context, **patch}, 2)

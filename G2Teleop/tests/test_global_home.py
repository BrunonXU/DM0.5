from __future__ import annotations

import numpy as np

from g2teleop.global_home import GlobalHomePlan


def test_global_home_moves_arms_first_then_hands_without_catchup_bursts() -> None:
    plan = GlobalHomePlan(
        np.zeros(14),
        np.ones(14),
        np.zeros(20),
        np.full(20, 0.24),
        started_at=10.0,
    )
    halfway = plan.step(12.5)
    np.testing.assert_allclose(halfway.arms, 0.5)
    np.testing.assert_allclose(halfway.hands, 0.0)
    assert not halfway.send_hands

    anchor = plan.step(15.0)
    assert anchor.phase == "hand_anchor"
    assert anchor.send_hands
    # A long scheduling gap advances only one point, never emits a burst or
    # skips straight to the final hand target.
    first = plan.step(16.0)
    assert first.send_hands
    np.testing.assert_allclose(first.hands, 0.03)
    assert not first.done

    now = 16.2
    final = first
    while not final.done:
        final = plan.step(now)
        now += 0.2
    np.testing.assert_allclose(final.arms, 1.0)
    np.testing.assert_allclose(final.hands, 0.24)
    assert final.phase == "complete"

from __future__ import annotations

import pytest

from g2teleop.control_timing import MinimumPeriodPacer


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        # Let the bounded spin loop make progress deterministically.
        value = self.now
        self.now += 0.0001
        return value

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_minimum_period_pacer_never_catches_up_after_lateness() -> None:
    clock = FakeClock()
    pacer = MinimumPeriodPacer(
        0.01,
        spin_guard_seconds=0.0005,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    first = pacer.wait()
    second = pacer.wait()
    assert second.started_at - first.started_at >= 0.01

    clock.now += 0.025
    late = pacer.wait()
    after_late = pacer.wait()
    assert late.lateness_seconds >= 0.015
    assert after_late.started_at - late.started_at >= 0.01


def test_minimum_period_pacer_validates_configuration() -> None:
    with pytest.raises(ValueError):
        MinimumPeriodPacer(0.0)
    with pytest.raises(ValueError):
        MinimumPeriodPacer(0.01, spin_guard_seconds=0.01)

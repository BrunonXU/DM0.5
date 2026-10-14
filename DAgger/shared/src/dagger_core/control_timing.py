"""Small timing primitives for the 100 Hz powered control owner."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable


@dataclass(frozen=True)
class PacerSample:
    started_at: float
    deadline: float
    lateness_seconds: float
    slept_seconds: float
    spun_seconds: float


class MinimumPeriodPacer:
    """Wait at least one period between control-loop starts without catch-up.

    Linux ``sleep`` commonly wakes a little late.  Sleeping until shortly before
    the deadline and spinning only for the final bounded guard interval reduces
    that overshoot.  The next deadline is always based on the actual start, so a
    late cycle is never followed by a short catch-up cycle.
    """

    def __init__(
        self,
        period_seconds: float,
        *,
        spin_guard_seconds: float = 0.0005,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if period_seconds <= 0.0:
            raise ValueError("period_seconds must be positive")
        if not 0.0 <= spin_guard_seconds < period_seconds:
            raise ValueError("spin_guard_seconds must be in [0, period_seconds)")
        self.period_seconds = float(period_seconds)
        self.spin_guard_seconds = float(spin_guard_seconds)
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_start: float | None = None

    def wait(self) -> PacerSample:
        now = self._monotonic()
        if self._last_start is None:
            self._last_start = now
            return PacerSample(now, now, 0.0, 0.0, 0.0)

        deadline = self._last_start + self.period_seconds
        sleep_started = now
        remaining = deadline - now
        if remaining > self.spin_guard_seconds:
            self._sleep(remaining - self.spin_guard_seconds)
        sleep_completed = self._monotonic()
        while True:
            started_at = self._monotonic()
            if started_at >= deadline:
                break
        self._last_start = started_at
        return PacerSample(
            started_at=started_at,
            deadline=deadline,
            lateness_seconds=max(0.0, started_at - deadline),
            slept_seconds=max(0.0, sleep_completed - sleep_started),
            spun_seconds=max(0.0, started_at - sleep_completed),
        )

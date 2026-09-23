from __future__ import annotations

import pytest

from g2_sort.validation.calibrate_rtc_schedule import build_calibration


def test_rtc_schedule_calibration_separates_latency_from_ds_quality_choice():
    result = build_calibration(
        [0.1, 0.2, 0.3],
        coverage_percentile=100.0,
        margin_steps=1,
    )

    assert result["real_delay_steps"]["max"] == 9
    assert result["latency_recommended_d"] == 10
    d5 = result["candidates"][5]
    assert d5["observed_at_or_before_d_fraction"] == pytest.approx(1 / 3)
    d10 = result["candidates"][10]
    assert d10["observed_at_or_before_d_fraction"] == 1.0
    s20 = next(item for item in d10["viable_s"] if item["s"] == 20)
    assert s20["committed_prefix_steps"] == 30
    assert s20["observed_schedule_miss_fraction"] == 0.0
    assert "trajectory A/B" in result["claim_boundary"]

from __future__ import annotations

import json

import numpy as np

from g2_sort.validation.evaluate_chunk_smoothing import (
    endpoint_constrained_regularization,
    load_executed_chunks,
    zero_phase_fir5,
)


def test_smoothers_preserve_endpoints_and_reduce_internal_impulse():
    arms = np.zeros((50, 14), dtype=np.float64)
    arms[24:, 5] = 0.08

    fir = zero_phase_fir5(arms)
    regularized = endpoint_constrained_regularization(
        arms, acceleration_weight=0.0, jerk_weight=10.0
    )

    for candidate in (fir, regularized):
        np.testing.assert_array_equal(candidate[[0, -1]], arms[[0, -1]])
        assert np.max(np.abs(np.diff(candidate[:, 5]))) < 0.08


def test_load_executed_chunks_joins_dispatch_and_skips_undispatched(tmp_path):
    actions = np.zeros((50, 34), dtype=np.float64).tolist()
    events = [
        {
            "event": "action_chunk",
            "request_id": 1,
            "actions": actions,
            "used_start_index": 0,
            "used_intervals": 49,
        },
        {
            "event": "dispatch_summary",
            "request_id": 1,
            "blend_seconds": 0.12,
        },
        {
            "event": "action_chunk",
            "request_id": 2,
            "actions": actions,
            "used_start_index": 0,
            "used_intervals": 49,
        },
    ]
    path = tmp_path / "telemetry.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events))

    chunks = load_executed_chunks(path)

    assert len(chunks) == 1
    assert chunks[0].blend_seconds == 0.12
    assert chunks[0].interval_count == 49

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts/12_g2_powered_f710.py"
    spec = importlib.util.spec_from_file_location("powered_f710_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stale_anchor_requires_exact_approved_home() -> None:
    check = _module().exact_approved_home_anchor
    positions = [0.1] * 20
    assert check({"positions": positions}, {"positions": positions, "approved": True})
    changed = positions.copy()
    changed[7] += 1e-12
    assert not check({"positions": changed}, {"positions": positions, "approved": True})
    assert not check({"positions": positions}, {"positions": positions, "approved": False})
    assert not check({"positions": positions[:-1]}, {"positions": positions, "approved": True})


def test_zero_feedback_anchor_accepts_fresh_validated_record() -> None:
    assess = _module().assess_zero_feedback_hand_anchor
    record = {"positions": [0.1] * 20, "wall_time": 2_000_000_000.0}
    result = assess(
        record,
        None,
        now_wall=2_000_000_100.0,
        max_age_s=200.0,
    )
    assert result["kind"] == "fresh_last_success"
    np.testing.assert_array_equal(result["positions"], np.full(20, 0.1))


def test_zero_feedback_anchor_rejects_stale_non_home() -> None:
    assess = _module().assess_zero_feedback_hand_anchor
    record = {"positions": [0.1] * 20, "wall_time": 2_000_000_000.0}
    approved = {"positions": [0.2] * 20, "approved": True}
    with pytest.raises(RuntimeError, match="stale.*not exactly"):
        assess(
            record,
            approved,
            now_wall=2_000_001_000.0,
            max_age_s=200.0,
        )


def test_zero_feedback_anchor_accepts_only_exact_stale_home() -> None:
    assess = _module().assess_zero_feedback_hand_anchor
    positions = [0.1] * 20
    record = {"positions": positions, "wall_time": 2_000_000_000.0}
    approved = {"positions": positions, "approved": True}
    result = assess(
        record,
        approved,
        now_wall=2_000_001_000.0,
        max_age_s=200.0,
    )
    assert result["kind"] == "stale_exact_approved_home"


def test_zero_feedback_anchor_rejects_invalid_shape() -> None:
    assess = _module().assess_zero_feedback_hand_anchor
    with pytest.raises(ValueError, match="finite 20D"):
        assess(
            {"positions": [0.1] * 19, "wall_time": 2_000_000_000.0},
            None,
            now_wall=2_000_000_100.0,
            max_age_s=200.0,
        )


def test_powered_loop_paces_the_real_dispatch_boundary() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "scripts/12_g2_powered_f710.py"
    ).read_text(encoding="utf-8")
    main_at = source.index("def main()")
    loop_at = source.index("while not shared.stop.is_set():", main_at)
    loop = source[loop_at:]
    solve_at = loop.index("arm_target.step(")
    pace_at = loop.index("pacer.wait()")
    send_at = loop.index("controller.send_action(dispatched)")
    assert solve_at < pace_at < send_at
    assert "dispatch_fresh" in loop[pace_at:send_at]
    assert "action[:14] = arm_action_before_tick" in loop[pace_at:send_at]


def test_f710_uses_changed_only_5hz_o10_without_periodic_hand_worker() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "scripts/12_g2_powered_f710.py"
    ).read_text(encoding="utf-8")
    assert "F710_HAND_HZ = 5.0" in source
    assert 'servo_output_mode="arms-only"' in source
    assert "ChangedOnlyHandScheduler(" in source
    assert "controller.send_hands_once(" in source
    assert '"f710_intent_changed_5hz_move_ee_pos_once"' in source
    assert "GlobalHomePlan(" in source
    assert '"f710_global_home_changed_5hz_move_ee_pos_once"' in source
    assert "controller.enable_split_hand_output()" not in source
    assert 'hand_smoothing_profile="v1_1"' not in source


def test_feedback_sidecar_only_finalizes_after_summary(tmp_path) -> None:
    finalize = _module().finalize_feedback_sidecar
    final = tmp_path / "feedback.jsonl"
    partial = Path(f"{final}.partial")
    partial.write_text(json.dumps({"event": "joint_feedback_sample"}) + "\n")
    assert not finalize(partial, final)
    assert partial.exists() and not final.exists()

    partial.write_text(
        json.dumps({"event": "joint_feedback_sample", "status": "ok", "joints_rad": [0.0] * 34})
        + "\n"
        + json.dumps({"event": "joint_feedback_summary"})
        + "\n"
    )
    assert finalize(partial, final)
    assert final.exists() and not partial.exists()


def test_feedback_summary_without_samples_is_not_published(tmp_path) -> None:
    finalize = _module().finalize_feedback_sidecar
    final = tmp_path / "feedback.jsonl"
    partial = Path(f"{final}.partial")
    partial.write_text(json.dumps({"event": "joint_feedback_summary"}) + "\n")
    assert not finalize(partial, final)
    assert partial.exists() and not final.exists()


def test_feedback_finalizer_discards_truncated_tail_prefix(tmp_path) -> None:
    finalize = _module().finalize_feedback_sidecar
    final = tmp_path / "feedback.jsonl"
    partial = Path(f"{final}.partial")
    partial.write_text(
        json.dumps({"event": "padding", "payload": "x" * 70000})
        + "\n"
        + "malformed-but-ignorable\n"
        + json.dumps(
            {
                "event": "joint_feedback_sample",
                "status": "ok",
                "joints_rad": [0.0] * 34,
            }
        )
        + "\n"
        + json.dumps({"event": "joint_feedback_summary"})
        + "\n",
        encoding="utf-8",
    )

    assert finalize(partial, final)
    assert final.exists() and not partial.exists()


def test_recover_stopped_main_telemetry_marks_synthetic_summary(tmp_path) -> None:
    recover = _module().recover_stopped_main_telemetry
    final = tmp_path / "powered.jsonl"
    partial = Path(f"{final}.partial")
    partial.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "telemetry_family": "f710_teleop",
                "event": "runtime_start",
                "run_id": "run-1",
                "sequence": 0,
            }
        )
        + "\n"
        + json.dumps(
            {
                "schema_version": 1,
                "telemetry_family": "f710_teleop",
                "event": "runtime_stop",
                "run_id": "run-1",
                "sequence": 1,
                "drop_count_before": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert recover(partial, final)
    records = [json.loads(line) for line in final.read_text().splitlines()]
    assert records[-1]["event"] == "runtime_summary"
    assert records[-1]["status"] == "recovered_after_feedback_finalizer_error"
    assert records[-1]["recovered"] is True
    assert not partial.exists()


def test_recovery_refuses_non_stopped_or_malformed_main(tmp_path) -> None:
    recover = _module().recover_stopped_main_telemetry
    for name, contents in (
        ("running", json.dumps({"event": "f710_control_sample"}) + "\n"),
        ("malformed", "not-json\n"),
    ):
        final = tmp_path / f"{name}.jsonl"
        partial = Path(f"{final}.partial")
        partial.write_text(contents, encoding="utf-8")
        assert not recover(partial, final)
        assert partial.exists() and not final.exists()

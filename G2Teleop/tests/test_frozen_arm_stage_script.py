from pathlib import Path


def test_frozen_stage_replay_can_isolate_arms_and_does_not_recompute_pipeline() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "scripts/21_g2_frozen_arm_stage_replay.py"
    ).read_text(encoding="utf-8")
    assert 'default="normal"' in source
    assert '"split" if cli.hand_mode != "disabled" else "arms-only"' in source
    assert '"arms14_normal_hands20_move_ee_pos"' in source
    assert 'else "arms14_only"' in source
    assert 'choices=("direct", "pink", "otg")' in source
    assert '"hold-current", "recorded-actions")' in source
    assert 'cli.arm_mode == "hold-current"' in source
    assert 'cli.hand_mode == "hold-current"' in source
    assert "hand start mismatch" not in source
    assert 'artifact.metadata.get("hand_command_source") != "actions"' in source
    assert '"frozen_arm_dispatch_sample"' in source
    assert "send_started_monotonic_ns" in source
    assert "send_completed_monotonic_ns" in source
    for forbidden in ("dual_eef_fk", "PoweredArmTarget", ".move_ee_pos(", "move_base"):
        assert forbidden not in source


def test_powered_shell_fixes_normal_arms_only_artifact_and_requires_phrase() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "scripts/23_g2_frozen_arm_stage_powered.sh"
    ).read_text(encoding="utf-8")
    assert "EXPECTED_SHA=31537fe9" in source
    assert '--servo-channel normal' in source
    assert '--execute' in source
    assert "Press Enter to start the countdown" in source
    assert "CONFIRM=" not in source
    assert 'for LEFT in 5 4 3 2 1' in source
    assert "low-latency" not in source
    assert '--hand-mode recorded-actions' in source


def test_o10_hold_probe_keeps_both_command_streams_static() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "scripts/24_g2_o10_hold_contention_probe.sh"
    ).read_text(encoding="utf-8")
    assert '--arm-mode hold-current' in source
    assert '--hand-mode hold-current' in source
    assert '--duration-seconds 20' in source
    assert '--servo-channel normal' in source
    assert "Press Enter to start the countdown" in source
    assert "RUN O10 HOLD" not in source


def test_dynamic_o10_probe_holds_measured_arms_and_replays_hands() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "scripts/25_g2_o10_dynamic_arm_hold_probe.sh"
    ).read_text(encoding="utf-8")
    assert "EXPECTED_SHA=31537fe9" in source
    assert '--arm-mode hold-current' in source
    assert '--hand-mode recorded-actions' in source
    assert '--servo-channel normal' in source
    assert "Press Enter to start the countdown" in source
    assert "RUN DYNAMIC O10" not in source
    assert "low-latency" not in source
    assert "--duration-seconds" not in source


def test_o10_point_target_ab_has_one_shot_and_stream_phases() -> None:
    root = Path(__file__).resolve().parents[1]
    python_source = (root / "scripts/26_g2_o10_point_target_ab.py").read_text(
        encoding="utf-8"
    )
    shell_source = (root / "scripts/27_g2_o10_point_target_ab.sh").read_text(
        encoding="utf-8"
    )
    assert '"a_final_once"' in python_source
    assert '"b_v1_1_10hz"' in python_source
    assert "send_hands_once" in python_source
    assert 'servo_output_mode="arms-only"' in python_source
    assert "Press Enter to start the countdown" in shell_source
    assert "RUN " not in shell_source


def test_o10_event_replay_uses_one_call_per_change_window() -> None:
    root = Path(__file__).resolve().parents[1]
    python_source = (root / "scripts/28_g2_o10_event_replay.py").read_text(
        encoding="utf-8"
    )
    shell_source = (root / "scripts/29_g2_o10_event_arm_hold.sh").read_text(
        encoding="utf-8"
    )
    assert "coalesce_hand_events" in python_source
    assert '"task_endpoint_once"' in python_source
    assert "send_hands_once" in python_source
    assert 'servo_output_mode="arms-only"' in python_source
    assert 'choices=("hold-current", "q-direct")' in python_source
    assert 'choices=("event-endpoint", "raw-5hz-changes")' in python_source
    assert "signals.q_direct[artifact_tick]" in python_source
    assert "Press Enter to start the countdown" in shell_source
    assert "RUN " not in shell_source


def test_qdirect_o10_event_shell_keeps_event_driven_hands() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "scripts/30_g2_qdirect_o10_event_replay.sh"
    ).read_text(encoding="utf-8")
    assert "EXPECTED_SHA=31537fe9" in source
    assert "--arm-mode q-direct" in source
    assert "scripts/28_g2_o10_event_replay.py" in source
    assert "Press Enter to start the countdown" in source
    assert "RUN " not in source
    assert "low-latency" not in source


def test_qdirect_raw5_shell_preserves_dataset_hand_timing() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "scripts/31_g2_qdirect_o10_raw5_replay.sh"
    ).read_text(encoding="utf-8")
    assert "EXPECTED_SHA=31537fe9" in source
    assert "--arm-mode q-direct" in source
    assert "--hand-execution raw-5hz-changes" in source
    assert "Unchanged 5 Hz hand samples are not resent" in source
    assert "Press Enter to start the countdown" in source
    assert "RUN " not in source
    assert "low-latency" not in source


def test_source_speed_replay_uses_original_json_timing() -> None:
    root = Path(__file__).resolve().parents[1]
    python_source = (root / "scripts/28_g2_o10_event_replay.py").read_text(
        encoding="utf-8"
    )
    shell_source = (
        root / "scripts/32_g2_source_speed_qdirect_o10_raw5.sh"
    ).read_text(encoding="utf-8")
    assert "--source-replay-json" in python_source
    assert 'timeline_mode="source-30hz-to-100hz"' in python_source
    assert "replay.states[:, joint]" in python_source
    assert "replay.actions[:, joint]" in python_source
    assert "EXPECTED_SHA=28233ac" in shell_source
    assert "--source-replay-json" in shell_source
    assert "Press Enter to start the countdown" in shell_source
    assert "RUN " not in shell_source

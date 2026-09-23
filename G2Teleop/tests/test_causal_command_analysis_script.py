from pathlib import Path


def test_causal_analysis_is_offline_and_compares_eef_to_direct() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "scripts/22_analyze_causal_command_artifact.py"
    ).read_text(encoding="utf-8")
    assert '"hardware_output": False' in source
    assert '"time_aligned_eef_error_vs_direct"' in source
    assert '"end_position_error_m"' in source
    assert "dual_eef_fk(" in source
    for forbidden in ("agibot_gdk", "G2RobotController", "joint_servo_control"):
        assert forbidden not in source

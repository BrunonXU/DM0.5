from __future__ import annotations

from pathlib import Path


def test_ruckig_shadow_has_no_motion_output_api() -> None:
    path = Path(__file__).resolve().parents[1] / "scripts/17_g2_ruckig_otg_shadow.py"
    source = path.read_text(encoding="utf-8")
    forbidden = (
        "joint_servo_control",
        "move_ee_pos",
        ".send_action(",
        "G2RobotController",
        "enable_split_hand_output",
        "emergency_stop",
    )
    for token in forbidden:
        assert token not in source
    assert 'hardware_output=False' in source

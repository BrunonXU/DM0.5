from __future__ import annotations

from pathlib import Path


def test_live_shadow_executable_has_no_hardware_command_api() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "10_g2_live_ik_shadow.py"
    ).read_text(encoding="utf-8")
    banned = ("joint_servo_control", "move_ee_pos", "send_action")
    assert all(name not in source for name in banned)
    assert "get_joint_states" in source
    assert "gdk_init" in source
    assert "gdk_release" in source

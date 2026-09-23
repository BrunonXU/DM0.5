from pathlib import Path


def test_chain_generator_is_offline_and_freezes_all_causal_stages() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "scripts/20_g2_bc_eef_chain_replay.py"
    ).read_text(encoding="utf-8")
    assert "Offline-only" in source or "offline-only" in source
    assert 'parser.add_argument("--execute"' not in source
    assert "agibot_gdk" not in source
    assert "G2RobotController" not in source
    assert 'choices=("states", "actions")' in source
    assert 'q_direct=np.stack(artifact_direct)' in source
    assert 'q_pink=np.stack(artifact_pink)' in source
    assert 'q_otg=np.stack(artifact_otg)' in source
    assert '"hand_command_source": "actions"' in source
    assert 'replay.actions[:, 14:]' in source
    assert 'choices=("source", "velocity-limited")' in source
    assert "PoweredArmTarget(" in source
    assert "dual_eef_fk(" in source
    assert "ideal_timed_twist(" in source
    for forbidden in ("move_base", "chassis_control", "set_velocity_base"):
        assert forbidden not in source


def test_shell_only_generates_offline_artifact() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "scripts/20_g2_bc_eef_chain_replay.sh"
    ).read_text(encoding="utf-8")
    preflight = source.index("--preflight-files-only")
    offline = source.index("--offline-artifact-npz", preflight)
    assert preflight < offline
    assert "read -r -p" not in source
    assert "--execute" not in source

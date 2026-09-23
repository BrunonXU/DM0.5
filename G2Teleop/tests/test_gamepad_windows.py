from __future__ import annotations

from g2teleop.gamepad_windows import WindowsF710Reader


class _Events:
    def __init__(self) -> None:
        self.get_calls = 0

    def get(self) -> list[object]:
        self.get_calls += 1
        return []


class _Pygame:
    def __init__(self) -> None:
        self.event = _Events()


class _Joystick:
    def get_init(self) -> bool:
        return True

    def get_hat(self, _index: int) -> tuple[int, int]:
        return 0, 0

    def get_axis(self, _index: int) -> float:
        return -1.0 if _index in {4, 5} else 0.0

    def get_button(self, _index: int) -> int:
        return 0


def test_snapshot_drains_windows_event_queue_each_sample() -> None:
    reader = WindowsF710Reader()
    pygame = _Pygame()
    reader._pygame = pygame
    reader._joystick = _Joystick()
    sample = reader.snapshot()
    assert sample.connected
    assert pygame.event.get_calls == 1


def test_expected_start_index_is_exposed_but_requires_field_verification() -> None:
    class StartJoystick(_Joystick):
        def get_button(self, index: int) -> int:
            return int(index == 7)

    reader = WindowsF710Reader()
    reader._pygame = _Pygame()
    reader._joystick = StartJoystick()
    assert reader.snapshot().start is True


def test_powered_runner_has_no_duplicate_full_state_read_at_takeover() -> None:
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[1] / "scripts/12_g2_powered_f710.py"
    ).read_text(encoding="utf-8")
    assert script.count("np.asarray(controller.read_state()") == 1


def test_sender_status_key_ignores_continuously_changing_axes() -> None:
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace

    path = Path(__file__).resolve().parents[1] / "scripts/03_windows_shadow_sender.py"
    spec = importlib.util.spec_from_file_location("windows_sender_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    first = SimpleNamespace(
        active_side="left", mode="combined", deadman=True, warning=None,
        axes=(0.0, 0.0, 0.0), angular_axes=(0.0, 0.0, 0.0),
    )
    second = SimpleNamespace(
        active_side="left", mode="combined", deadman=True, warning=None,
        axes=(1.0, -1.0, 0.5), angular_axes=(0.2, 0.3, 0.4),
    )
    ack = SimpleNamespace(receiver_state="POWERED_ACTIVE", accepted=True, reason=None)
    assert module.operator_status_key(first, ack) == module.operator_status_key(second, ack)

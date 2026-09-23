import numpy as np

from g2_sort.operations.hardware_tests.training_data_replay import (
    arm_start_error,
    build_arms_only_actions,
    hand_feedback_is_usable,
    hand_start_error,
    load_replay,
)


def test_frozen_replay_contract() -> None:
    payload = load_replay()
    states = np.asarray(payload["states"])
    actions = np.asarray(payload["actions"])
    assert payload["episode_id"] == "sorting_20260409161901"
    assert states.shape == actions.shape == (948, 34)
    assert np.all(np.isfinite(actions))


def test_arms_only_preserves_measured_hands() -> None:
    payload = load_replay()
    measured = np.asarray(payload["states"][0]).copy()
    measured[14:] += np.linspace(-0.01, 0.01, 20)
    actions = build_arms_only_actions(payload["actions"], measured)
    np.testing.assert_array_equal(actions[:, :14], payload["actions"][:, :14])
    np.testing.assert_allclose(actions[:, 14:], np.tile(measured[None, 14:], (948, 1)))


def test_start_error_uses_arms_only() -> None:
    expected = np.zeros(34)
    measured = expected.copy()
    measured[2] = 0.04
    measured[20] = 1.00
    assert arm_start_error(measured, expected) == 0.04
    assert hand_start_error(measured, expected) == 1.00


def test_all_zero_hand_feedback_is_not_a_start_pose() -> None:
    measured = np.zeros(34)
    measured[:14] = np.linspace(-1.0, 1.0, 14)
    assert not hand_feedback_is_usable(measured)
    measured[15] = 0.925793
    assert hand_feedback_is_usable(measured)


def test_unified_replay_routes_full_actions_and_releases_owner(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    import g2_sort.operations.hardware_tests.training_data_replay as replay
    payload = load_replay()
    captured = {}
    class Controller:
        def __init__(self, **kwargs): captured['config'] = kwargs
        def read_state(self): return payload['actions'][0].copy()
        def send_action(self, action): captured.setdefault('sent', []).append(action.copy())
        def control_stats(self): return {'request_layout': 'unified-34'}
        def emergency_stop(self): pass
        def close(self): captured['closed'] = True
    class Servo:
        def __init__(self, send, anchor, **kwargs): self.send = send
        def start(self): pass
        def play(self, actions, **kwargs):
            captured['actions'] = actions.copy()
            self.send(actions[1]); return 1
        def is_playing(self, playback_id): return False
        def hold(self, target): self.send(target)
        def close(self): pass
    monkeypatch.setattr(replay, 'G2RobotController', Controller)
    monkeypatch.setattr(replay, 'ServoStreamer', Servo)
    monkeypatch.setattr(replay, 'confirm_enter_and_countdown', lambda *a, **k: None)
    monkeypatch.setattr(replay.time, 'sleep', lambda _: None)
    monkeypatch.setitem(sys.modules, 'agibot_gdk', SimpleNamespace(
        gdk_init=lambda:0, GDKRes=SimpleNamespace(kSuccess=0), gdk_release=lambda:None))
    lock = tmp_path / 'owner.pid'
    monkeypatch.setenv('G2_RUNTIME_PID_FILE', str(lock))
    monkeypatch.setattr(sys, 'argv', ['replay', '--unified-34', '--execute'])
    assert replay.run() == 0
    assert captured['config']['servo_output_mode'] == 'unified-34'
    assert captured['config']['servo_channel'] == 'normal'
    np.testing.assert_array_equal(captured['actions'], payload['actions'])
    assert all(a.shape == (34,) for a in captured['sent'])
    assert captured['closed'] and not lock.exists()


def test_files_only_never_imports_gdk(monkeypatch):
    import sys
    import g2_sort.operations.hardware_tests.training_data_replay as replay
    monkeypatch.setitem(sys.modules, 'agibot_gdk', None)
    monkeypatch.setattr(sys, 'argv', ['replay', '--unified-34', '--files-only'])
    assert replay.run() == 0


def test_hand_preparation_holds_arms_and_preserves_source():
    from g2_sort.operations.hardware_tests.training_data_replay import build_hand_start_preparation
    target = load_replay()['actions'][0].copy()
    start = target.copy()
    start[14:24] = [-.2,1.45,-.75,0,1,1,0,1,0,1]
    before = start.copy()
    trajectory = build_hand_start_preparation(start, target)
    assert trajectory.shape == (151,34)
    np.testing.assert_array_equal(start, before)
    np.testing.assert_array_equal(trajectory[0], start)
    np.testing.assert_allclose(trajectory[-1,14:], target[14:])
    np.testing.assert_array_equal(trajectory[:,:14], np.tile(start[:14],(151,1)))


def test_hand_preparation_rejects_stuck_feedback(monkeypatch):
    import pytest
    import g2_sort.operations.hardware_tests.training_data_replay as replay
    target = load_replay()['actions'][0].copy()
    start = target.copy(); start[18] = 1.
    class Servo:
        def play(self, *args, **kwargs): return 1
        def is_playing(self, *args): return False
        def hold(self, target): pass
        def has_fault(self): return False
    class Robot:
        def read_state(self): return start.copy()
    ticks = iter(range(30))
    monkeypatch.setattr(replay.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(replay.time, 'sleep', lambda _: None)
    report = {}
    with pytest.raises(RuntimeError, match='dataset playback NOT started'):
        replay.prepare_hand_start(Robot(), Servo(), start, target, report)
    assert not report['hand_preparation']['verified']


def test_hand_preparation_accepts_reached_target(monkeypatch):
    import g2_sort.operations.hardware_tests.training_data_replay as replay
    from types import SimpleNamespace
    target = load_replay()['actions'][0].copy()
    start = target.copy(); start[18] = 1.
    servo = SimpleNamespace(play=lambda *a,**k:1, is_playing=lambda _:False,
                            hold=lambda _:None, has_fault=lambda:False)
    robot = SimpleNamespace(read_state=lambda:target.copy())
    report = {}
    replay.prepare_hand_start(robot, servo, start, target, report)
    assert report['hand_preparation']['verified']

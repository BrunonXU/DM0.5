"""Input diagnostic regressions: no pygame, Windows, network, or hardware."""
import ctypes
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def probe():
    path = Path(__file__).parents[1] / 'windows_client/scripts/04_windows_f1_input_probe.py'
    spec = importlib.util.spec_from_file_location('f1_input_probe_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_xinput_getstate_abi_and_four_slots(probe):
    assert ctypes.sizeof(probe.XInputGamepad) == 12
    assert ctypes.sizeof(probe.XInputState) == 16
    assert probe.XInputState.gamepad.offset == 4
    calls = []
    def get_state(slot, pointer):
        calls.append(slot)
        if slot != 2:
            return 1167
        state = pointer._obj
        state.packet = 419
        state.gamepad.buttons = 0x0010 | 0x0200 | 0x0004
        state.gamepad.left_x = -32768
        return 0
    reader = probe.ReadOnlyXInput()
    reader._get_state = get_state
    states = reader.sample()
    assert calls == [0, 1, 2, 3]
    assert [v['connected'] for v in states] == [False, False, True, False]
    assert states[2]['packet'] == 419
    assert states[2]['pressed'] == ['DPAD_LEFT', 'START', 'RB']
    assert states[2]['axes'][0] == -32768


def test_collect_actual_reader_mapper_edges_and_bounded_console(probe):
    from g2dagger.f710_mapping import GamepadSnapshot, F710Mapper
    samples = []
    now = [0.]
    inputs = [False, True, False, True, False]
    class Joystick:
        def get_numbuttons(self): return 8
        def get_numaxes(self): return 6
        def get_numhats(self): return 1
        def get_button(self, index): return int(index == 7 and reader.start)
        def get_axis(self, index): return -1. if index in (4, 5) else 0.
        def get_hat(self, index): return (0, 0)
    class Reader:
        _joystick = Joystick()
        start = False
        calls = 0
        def snapshot(self):
            self.start = inputs[self.calls] if self.calls < len(inputs) else False
            self.calls += 1
            return GamepadSnapshot(start=self.start)
    reader = Reader()
    printed = []
    def sleep(seconds): now[0] += max(seconds, .0001)
    probe.collect_samples(reader, F710Mapper(), SimpleNamespace(sample=lambda: []), 28., samples,
                          clock=lambda: now[0], sleep=sleep, emit=printed.append)
    summary = probe.summarize(samples)
    assert summary['reader_start_rising_edges'] == 2
    assert summary['mapper_toggle_max'] == 2
    assert summary['sdl_buttons_seen'] == [7]
    assert summary['sdl_raw_changes'] == 4
    assert summary['reader_connected_samples'] == len(samples)
    assert samples[1]['reader']['start'] and samples[1]['sdl']['buttons'][7]
    assert len(printed) <= 25
    assert len([line for line in printed if 'START 约' in line]) == 1


def test_disconnected_reader_does_not_hide_raw_sdl_evidence(probe):
    from g2dagger.f710_mapping import GamepadSnapshot, F710Mapper
    class RawJoystick:
        def get_numbuttons(self): return 8
        def get_numaxes(self): return 2
        def get_numhats(self): return 0
        def get_button(self, i): return int(i == 7)
        def get_axis(self, i): return .25
    raw = probe.raw_sdl_sample(SimpleNamespace(_joystick=RawJoystick()))
    snapshot = GamepadSnapshot(connected=False)
    from dataclasses import asdict
    sample = {'elapsed_s': 0., 'sdl': raw, 'xinput': [],
              'reader': asdict(snapshot), 'mapped': asdict(F710Mapper().map(snapshot))}
    summary = probe.summarize([sample])
    assert summary['reader_connected_samples'] == 0
    assert summary['sdl_buttons_seen'] == [7]
    assert summary['mapper_toggle_max'] == 0
    assert summary['sdl_hats_seen'] == []
    assert summary['interpretation'] == 'observations only; no automatic cause assigned'


def test_file_evidence_matches_bytes_and_missing_file_is_explicit(probe, tmp_path):
    file = tmp_path / 'reader.py'
    file.write_bytes(b'known implementation\n')
    import hashlib
    assert probe.file_fingerprint(file) == {'path': str(file),
        'sha256': hashlib.sha256(file.read_bytes()).hexdigest()}
    assert 'error' in probe.file_fingerprint(tmp_path / 'missing.py')

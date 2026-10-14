"""F1 wrapper tests: no pygame, console API, network or robot is required."""
from contextlib import nullcontext
import ctypes
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS

import pytest


@pytest.fixture
def wrapper():
    path = Path(__file__).parents[1]/'windows_client/scripts/03_windows_f1_sender.py'
    spec = importlib.util.spec_from_file_location('test_f1_sender_entry', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reader_ready_before_transport_start_and_original_loop_reused(wrapper, capsys):
    sender = wrapper.load_sender()
    original_main = sender.main
    from g2dagger import sender_guidance
    original_guidance = sender_guidance.operator_guidance
    events = []

    class Reader:
        name = 'mock F710'
        def __init__(self, index):
            assert index == 2
        def open(self):
            events.append('gamepad_open')
        def snapshot(self):
            events.append('gamepad_sample')
            raise KeyboardInterrupt
        def close(self):
            events.append('gamepad_close')

    class Transport:
        def __init__(self, host, port, *, ack_timeout_s, status):
            assert (host, port, ack_timeout_s) == ('127.0.0.1', 7907, .5)
            events.append('transport_constructed')
        def start(self):
            assert 'gamepad_open' in events
            events.append('transport_start')
        def snapshot(self):
            return NS(connected=True, generation=1)
        def close(self):
            events.append('transport_close')

    sender.WindowsF710Reader = Reader
    sender.AsyncLatestIntentTransport = Transport
    sender.WindowsTimerResolution = nullcontext
    sender.parse_args = lambda: NS(host='127.0.0.1', port=7907, ack_timeout_ms=500., index=2,
                                   hz=50., log=Path('unused.log'), verbose=False)
    sender.install_console_log = lambda path: None
    wrapper.configure_sender(sender)
    assert sender.main is original_main
    assert sender_guidance.operator_guidance is original_guidance
    assert sender.main() == 0
    assert events == ['transport_constructed', 'gamepad_open', 'transport_start',
                      'gamepad_sample', 'transport_close', 'gamepad_close']
    text = capsys.readouterr().out
    assert 'G2' not in text and 'DM0.5' not in text
    assert 'SHADOW 只计算和记录，不输出动作' in text
    assert '纯人工入口首次 START 进入人工' in text


def test_hotplug_reuses_transport_and_cleanup_on_initialization_error(wrapper):
    sender = wrapper.load_sender()
    events = []
    class Transport:
        def __init__(self): pass
        def start(self): events.append('start')
        def close(self): events.append('close')
    sender.AsyncLatestIntentTransport = Transport
    sender.wait_for_gamepad = lambda reader: reader.open()
    wrapper.configure_sender(sender)
    reader = NS(open=lambda: events.append('open'))
    with sender.AsyncLatestIntentTransport():
        sender.wait_for_gamepad(reader)
        sender.wait_for_gamepad(reader)
    assert events == ['open', 'start', 'open', 'close']
    events.clear()
    def fail(): raise RuntimeError('pygame failed')
    with pytest.raises(RuntimeError, match='pygame failed'):
        with sender.AsyncLatestIntentTransport():
            sender.wait_for_gamepad(NS(open=fail))
    assert events == ['close']


class Kernel32:
    def __init__(self, *, handle=7, get_ok=True, set_ok=True):
        self.handle, self.get_ok, self.set_ok = handle, get_ok, set_ok
        self.original = 0x01F7
        self.written = []
    def GetStdHandle(self, which):
        assert which == -10
        return self.handle
    def GetConsoleMode(self, handle, mode):
        assert handle == self.handle
        mode._obj.value = self.original
        return self.get_ok
    def SetConsoleMode(self, handle, mode):
        assert handle == self.handle
        self.written.append(mode)
        return self.set_ok


def test_quickedit_restores_exact_original_mode_after_failure(wrapper):
    api = Kernel32()
    with pytest.raises(RuntimeError, match='stop'):
        with wrapper.WindowsQuickEditGuard(platform='win32', kernel32=api) as guard:
            assert guard.changed
            assert api.written == [(api.original | 0x80) & ~0x40]
            raise RuntimeError('stop')
    assert api.written[-1] == api.original
    assert not guard.changed


@pytest.mark.parametrize('platform,kwargs', [
    ('linux', {}), ('win32', {'handle': None}),
    ('win32', {'handle': ctypes.c_void_p(-1).value}), ('win32', {'get_ok': False}),
])
def test_quickedit_unsupported_consoles_are_noop(wrapper, platform, kwargs):
    api = Kernel32(**kwargs)
    with wrapper.WindowsQuickEditGuard(platform=platform, kernel32=api) as guard:
        assert not guard.changed
    assert not api.written


def test_quickedit_set_failure_does_not_claim_success(wrapper):
    api = Kernel32(set_ok=False)
    with wrapper.WindowsQuickEditGuard(platform='win32', kernel32=api) as guard:
        assert not guard.changed
    assert len(api.written) == 1


def test_rejected_manual_ack_is_not_presented_as_authorized(wrapper):
    sender = wrapper.configure_sender(wrapper.load_sender())
    ack = NS(accepted=False, reason='powered_hil:waiting_for_teleop_start')
    assert '[输入被拒绝]' in sender.operator_guidance(None, ack)
    ack.accepted = True
    assert '执行模式未声明' in sender.operator_guidance(None, ack)


def test_missing_ack_points_to_receiver_failure_without_claiming_gamepad_failure(wrapper):
    sender = wrapper.configure_sender(wrapper.load_sender())
    text = sender.operator_guidance(None, None)
    assert 'failure.txt' in text and '尚未确认 F1 接收程序存活' in text
    text = sender.transport_guidance('pending packet ACK overdue')
    assert '本地 SSH 能连不代表远端 7906 正在监听' in text
    assert 'ACK 超时不证明手柄掉线' in text


@pytest.mark.parametrize('metadata', ['', ';release=20261009-f1-hgdagger-v14-r1;source=abc;config=def'])
@pytest.mark.parametrize('mode', ['shadow', 'powered'])
@pytest.mark.parametrize('state,guidance', [
    ('waiting_for_teleop_start', '[F1 等待人工开始]'),
    ('manual_hold', '[F1 人工暂停]'),
    ('hil_latched', '[人工介入 HIL / 左侧]'),
    ('safe_hold', '[安全暂停 SAFE_HOLD]'),
    ('policy', '[同步策略模式 已配置策略]'),
    ('handoff_hold', '[等待新策略]'),
])
def test_ack_execution_mode_survives_protocol_and_retains_input_guidance(wrapper, mode, state, guidance, metadata):
    from g2dagger.protocol import AckPacket, decode_ack, encode_packet
    sender = wrapper.configure_sender(wrapper.load_sender())
    original = AckPacket('f1-test', 4, True, 'HOLD',
        'powered_hil:' + state + metadata + ';execution_mode=' + mode)
    ack = decode_ack(encode_packet(original))
    intent = NS(active_side='left', deadman=True, warning=None)
    text = sender.operator_guidance(intent, ack)
    assert guidance in text
    assert ('POWERED' if mode == 'shadow' else 'SHADOW') not in text
    assert '执行模式未声明' not in text
    assert ('[F1 SHADOW 无动作输出]' if mode == 'shadow' else
            '[F1 POWERED 执行入口；ACK 不证明运动]') in text
    assert ack == original


def test_old_f1_receiver_and_rejected_ack_still_work(wrapper):
    from g2dagger.protocol import AckPacket
    sender = wrapper.configure_sender(wrapper.load_sender())
    ack = AckPacket('f1-test', 4, True, 'HOLD', 'powered_hil:safe_hold')
    assert 'SAFE_HOLD' in sender.operator_guidance(None, ack)
    assert '执行模式未声明' in sender.operator_guidance(None, ack)
    rejected = AckPacket('f1-test', 5, False, 'HOLD', 'invalid_sequence')
    text = sender.operator_guidance(None, rejected)
    assert '[输入被拒绝]' in text and 'POWERED 执行入口' not in text

"""F1 wrapper tests: no pygame, console API, network or robot is required."""
from contextlib import nullcontext
import ctypes
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS

import pytest


@pytest.fixture
def wrapper():
    import sys
    original_path = sys.path[:]
    path = Path(__file__).parents[1]/'windows_client/scripts/03_windows_f1_sender.py'
    spec = importlib.util.spec_from_file_location('test_f1_sender_entry', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.path[:] = original_path


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
        def __init__(self, host, port, *, ack_timeout_s, progress_timeout_s, status):
            assert progress_timeout_s == 1.0
            assert (host, port, ack_timeout_s) == ('127.0.0.1', 7907, .5)
            events.append('transport_constructed')
        def start(self):
            assert 'gamepad_open' in events
            events.append('transport_start')
        def snapshot(self):
            return NS(connected=True, generation=1, last_ack=None)
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
    assert '默认右臂，无需十字键选侧' in text


def test_hotplug_reuses_transport_and_cleanup_on_initialization_error(wrapper):
    sender = wrapper.load_sender()
    events = []
    class Transport:
        def __init__(self, **kwargs): assert kwargs == dict(progress_timeout_s=1.0)
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


@pytest.fixture
def right_arm_sender(wrapper):
    return wrapper.configure_sender(wrapper.load_sender())


def test_default_right_arm_start_then_rb_and_wire_packet(right_arm_sender):
    from dagger_core.f710_mapping import GamepadSnapshot as Sample
    from dagger_core.protocol import decode_intent, encode_packet
    mapper = right_arm_sender.F710Mapper()
    neutral = mapper.map(Sample())
    assert neutral.active_side == mapper.active_side == 'right'
    assert not neutral.deadman and neutral.grip_target is None
    start = mapper.map(Sample(start=True))
    assert start.hil_toggle_id == 1 and start.hil_button_pressed
    assert not start.deadman and start.grip_target is None
    mapper.map(Sample())
    enabled = mapper.map(Sample(rb=True))
    assert enabled.deadman and enabled.active_side == 'right'
    assert enabled.grip_target is None
    motion = mapper.map(Sample(rb=True, left_y=-.5))
    assert motion.axes[0] > 0
    packet = right_arm_sender.make_packet('f1-right-arm', 3, motion)
    decoded = decode_intent(encode_packet(packet))
    assert decoded.active_side == 'right' and decoded.deadman
    assert decoded.axes == motion.axes and decoded.grip_target is None
    assert decoded.hil_toggle_id == 1


def test_f1_mapper_does_not_change_shared_or_fresh_sender(wrapper, right_arm_sender):
    from dagger_core.f710_mapping import F710Mapper, GamepadSnapshot as Sample
    assert wrapper.load_sender().F710Mapper is F710Mapper
    shared = F710Mapper()
    assert shared.active_side is None
    shared.map(Sample())
    assert shared.map(Sample(dpad_x=-1)).active_side == 'left'
    assert right_arm_sender.F710Mapper().active_side == 'right'


@pytest.mark.parametrize('dpad', [(-1, 0), (1, 0), (0, 1), (0, -1), (-1, 1)])
def test_dpad_never_selects_left_and_blocks_simultaneous_rb(right_arm_sender, dpad):
    from dagger_core.f710_mapping import GamepadSnapshot as Sample
    mapper = right_arm_sender.F710Mapper()
    mapper.map(Sample())
    pad = dict(dpad_x=dpad[0], dpad_y=dpad[1])
    selected = mapper.map(Sample(**pad))
    assert selected.active_side == mapper.active_side == 'right'
    assert not selected.deadman and selected.grip_target is None
    rejected = mapper.map(Sample(rb=True, **pad))
    assert not rejected.deadman
    assert rejected.warning == 'release_dpad_then_press_deadman'
    assert rejected.active_side == 'right'
    assert not mapper.map(Sample(rb=True)).deadman  # Releasing D-pad is insufficient.
    mapper.map(Sample())
    assert mapper.map(Sample(rb=True)).deadman


@pytest.mark.parametrize('dpad_x', [-1, 1])
def test_dpad_during_rb_keeps_existing_right_control_and_gripper(right_arm_sender, dpad_x):
    from dagger_core.f710_mapping import GamepadSnapshot as Sample
    mapper = right_arm_sender.F710Mapper()
    mapper.map(Sample())
    mapper.map(Sample(rb=True))
    intent = mapper.map(Sample(rb=True, dpad_x=dpad_x, left_y=-.5, right_trigger=1.))
    assert intent.deadman and intent.active_side == mapper.active_side == 'right'
    assert intent.axes[0] > 0 and intent.grip_target == 1.
    assert intent.warning == 'select_side_before_deadman'
    assert mapper._grip_target_by_side['left'] is None


def test_rb_held_at_startup_needs_neutral_and_new_edge(right_arm_sender):
    from dagger_core.f710_mapping import GamepadSnapshot as Sample
    mapper = right_arm_sender.F710Mapper()
    assert not mapper.map(Sample(rb=True)).deadman
    assert not mapper.map(Sample(rb=True, left_x=.5)).deadman
    mapper.map(Sample(left_x=.5))
    assert not mapper.map(Sample(rb=True)).deadman
    mapper.map(Sample())
    assert mapper.map(Sample(rb=True)).deadman


@pytest.mark.parametrize('reconnect', [False, True])
def test_held_rt_at_startup_or_reconnect_cannot_change_gripper(right_arm_sender, reconnect):
    from dagger_core.f710_mapping import GamepadSnapshot as Sample
    mapper = right_arm_sender.F710Mapper()
    if reconnect:
        mapper.map(Sample())
        assert mapper.map(Sample(right_trigger=1.)).grip_target == 1.
        disconnected = mapper.map(Sample(connected=False, right_trigger=1.))
        assert not disconnected.deadman and disconnected.grip_target is None
        assert disconnected.active_side is None
        assert mapper.active_side == 'right'
        mapper.reset_for_disconnect()  # TCP generation change follows the same path.
    for sample in (Sample(right_trigger=1.), Sample(rb=True, right_trigger=1.),
                   Sample(rb=True), Sample(rb=True, right_trigger=1.)):
        intent = mapper.map(sample)
        assert intent.active_side == 'right'
        assert not intent.deadman and intent.grip_target is None
    mapper.map(Sample())
    assert mapper.map(Sample(rb=True)).deadman
    assert mapper.map(Sample(right_trigger=1.)).grip_target == 1.


def test_home_retains_no_motion_request_clears_grip_then_defaults_right(right_arm_sender):
    from dagger_core.f710_mapping import GamepadSnapshot as Sample
    mapper = right_arm_sender.F710Mapper()
    mapper.map(Sample())
    assert mapper.map(Sample(right_trigger=1.)).grip_target == 1.
    home = mapper.map(Sample(y=True, right_trigger=1.))
    assert home.home_reset and not home.deadman
    assert home.active_side is None and home.grip_target is None
    assert home.axes == home.angular_axes == (0., 0., 0.)
    assert mapper.active_side == 'right'
    held_trigger = mapper.map(Sample(right_trigger=1.))
    assert held_trigger.active_side == 'right' and held_trigger.grip_target is None
    mapper.map(Sample())
    enabled = mapper.map(Sample(rb=True))
    assert enabled.deadman and enabled.active_side == 'right'
    assert enabled.grip_target is None
    assert mapper.map(Sample(right_trigger=1.)).grip_target == 1.


def test_home_while_rb_held_remains_rejected(right_arm_sender):
    from dagger_core.f710_mapping import GamepadSnapshot as Sample
    mapper = right_arm_sender.F710Mapper()
    mapper.map(Sample())
    mapper.map(Sample(rb=True))
    rejected = mapper.map(Sample(rb=True, y=True))
    assert not rejected.home_reset
    assert rejected.warning == 'release_deadman_before_home_reset'
    assert rejected.active_side == 'right'


def test_start_still_requires_real_release_and_edge_after_reconnect(right_arm_sender):
    from dagger_core.f710_mapping import GamepadSnapshot as Sample
    mapper = right_arm_sender.F710Mapper()
    assert mapper.map(Sample(start=True)).hil_toggle_id == 0
    mapper.map(Sample())
    assert mapper.map(Sample(start=True)).hil_toggle_id == 1
    assert mapper.map(Sample(start=True)).hil_toggle_id == 1
    mapper.reset_for_disconnect()
    assert mapper.map(Sample(start=True)).hil_toggle_id == 1
    mapper.map(Sample())
    assert mapper.map(Sample(start=True)).hil_toggle_id == 2


def test_f1_guidance_no_longer_requests_side_selection(right_arm_sender):
    from dagger_core.protocol import AckPacket
    for reason, intent in (
        ('safe_hold', None),
        ('hil_latched', NS(active_side='right', deadman=False, warning='release_dpad_then_press_deadman')),
        ('hil_latched', NS(active_side=None, deadman=False, warning=None)),
    ):
        text = right_arm_sender.operator_guidance(
            intent, AckPacket('f1-right-arm', 1, True, 'HOLD', 'powered_hil:' + reason))
        assert '选侧' not in text


@pytest.mark.parametrize('state', [
    'input_wait_release_rb', 'input_wait_fresh_packet', 'input_wait_press_rb',
    'input_wait_feedback', 'input_wait_update_sender',
])
def test_shared_input_pause_guidance_does_not_claim_a_recording_route(right_arm_sender, state):
    from dagger_core.protocol import AckPacket
    # The same ACK is used by free teleop, demonstration and policy HIL.
    ack = AckPacket('f1-policy-hil', 1, True, 'HOLD',
                    'powered_hil:' + state + ';execution_mode=powered')
    text = right_arm_sender.operator_guidance(None, ack)
    assert '录制' not in text and '结束本条' not in text
    assert 'RB' in text


def test_up_edges_release_reconnect_and_wire(right_arm_sender):
    from dagger_core.f710_mapping import GamepadSnapshot as Sample
    from dagger_core.protocol import decode_intent, encode_packet
    m = right_arm_sender.F710Mapper()
    assert m.map(Sample(dpad_y=1)).episode_end_id == 0
    m.map(Sample())
    up = m.map(Sample(dpad_y=1))
    assert up.episode_end_id == 1 and up.episode_end_pressed
    assert m.map(Sample(dpad_y=1)).episode_end_id == 1
    wire = right_arm_sender.make_packet('up', 1, up)
    assert decode_intent(encode_packet(wire)).episode_end_id == 1
    m.reset_for_disconnect()
    assert m.map(Sample(dpad_y=1)).episode_end_id == 1
    m.map(Sample())
    assert m.map(Sample(dpad_y=1, rb=True)).episode_end_id == 1
    assert m.map(Sample(dpad_y=1)).episode_end_id == 1
    m.map(Sample())
    assert m.map(Sample(dpad_y=1)).episode_end_id == 2


def test_legacy_wire_has_no_optional_episode_fields():
    from dagger_core.protocol import encode_packet, decode_intent, IntentPacket
    wire = encode_packet(IntentPacket('legacy', 0, 1, False, None, 'translation', (0.,0.,0.), 0., False))
    assert b'episode_end' not in wire
    assert decode_intent(wire).episode_end_id is None


def test_v4_down_is_explicit_failure_and_requires_release_before_next_rating(wrapper):
    from dagger_core.f710_mapping import GamepadSnapshot as Sample
    from dagger_core.protocol import encode_packet, decode_intent
    state = NS(connected=True, last_ack=NS(reason='powered_hil:episode_ready;rlt_gamepad_v1=1;rlt_local_v4=1'))
    sender = wrapper.load_sender()
    sender.AsyncLatestIntentTransport = lambda **kwargs: NS(snapshot=lambda: state, close=lambda: None)
    wrapper.configure_sender(sender)
    with sender.AsyncLatestIntentTransport():
        mapper = sender.F710Mapper()
        assert mapper.map(Sample(dpad_y=-1)).episode_end_id == 0
        mapper.map(Sample())
        down = mapper.map(Sample(dpad_y=-1))
        packet = decode_intent(encode_packet(sender.make_packet('v4', 0, down)))
        assert packet.operator_protocol == 'f1_rlt_local_v4'
        assert packet.local_result == 'failure' and packet.episode_end_id == 1
        assert mapper.map(Sample(dpad_y=-1)).episode_end_id == 1
        assert mapper.map(Sample(dpad_y=1)).episode_end_id == 1
        released = mapper.map(Sample())
        assert released.local_result == 'failure' and released.episode_end_id == 1
        up = mapper.map(Sample(dpad_y=1))
        assert up.local_result == 'success' and up.episode_end_id == 2
        mapper.map(Sample())
        assert mapper.map(Sample(dpad_y=-1, start=True)).episode_end_id == 2


def test_rlt_capability_negotiation_edges_and_legacy_wire(wrapper):
    from dagger_core.f710_mapping import GamepadSnapshot as Sample
    from dagger_core.protocol import encode_packet, decode_intent
    state = NS(connected=True, last_ack=None)
    sender = wrapper.load_sender()
    sender.AsyncLatestIntentTransport = lambda **kwargs: NS(snapshot=lambda: state, close=lambda: None)
    wrapper.configure_sender(sender)
    with sender.AsyncLatestIntentTransport():
        mapper = sender.F710Mapper()
        old = sender.make_packet('test-rlt', 0, mapper.map(Sample()))
        assert b'policy_switch' not in encode_packet(old)
        state.last_ack = NS(reason='powered_hil:episode_ready;rlt_gamepad_v1=1')
        assert mapper.map(Sample(dpad_x=1)).policy_switch_id == 0  # Already held at negotiation.
        mapper.map(Sample())
        intent = mapper.map(Sample(dpad_x=1))
        assert intent.policy_switch_id == 1
        assert mapper.map(Sample(dpad_x=1)).policy_switch_id == 1
        decoded = decode_intent(encode_packet(sender.make_packet('test-rlt', 1, intent)))
        assert decoded.policy_switch_id == 1 and decoded.policy_switch_pressed
        for conflict in ({'rb': True}, {'start': True}, {'y': True}):
            mapper.map(Sample())
            assert mapper.map(Sample(dpad_x=1, **conflict)).policy_switch_id == 1
        mapper.reset_for_disconnect()
        assert mapper.map(Sample(dpad_x=1)).policy_switch_id == 1
        mapper.map(Sample())
        assert mapper.map(Sample(dpad_x=1)).policy_switch_id == 2
        state.last_ack = NS(reason='powered_hil:episode_ready')
        assert b'policy_switch' not in encode_packet(sender.make_packet('test-rlt', 2, mapper.map(Sample())))

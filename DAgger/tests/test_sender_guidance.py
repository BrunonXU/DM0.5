from types import SimpleNamespace
from g2dagger.sender_guidance import operator_guidance, transport_guidance


def test_safe_hold_overrides_local_rb_and_home():
    intent = SimpleNamespace(active_side='left', deadman=True, warning=None, home_reset=True)
    ack = SimpleNamespace(accepted=True, reason='powered_hil:safe_hold', receiver_state='HOLD')
    message = operator_guidance(intent, ack)
    assert 'RB / Y 不能恢复' in message and 'START' in message
    assert '臂控制请求有效' not in message


def test_no_ack_or_unknown_receiver_does_not_claim_policy():
    intent = SimpleNamespace(active_side=None, deadman=False, warning=None)
    assert '尚未确认' in operator_guidance(intent, None)
    ack = SimpleNamespace(accepted=True, reason='something_new', receiver_state='HOLD')
    assert '不推断 START' in operator_guidance(intent, ack)
    ack.accepted = False
    ack.reason = 'powered_hil:policy'
    assert '输入被拒绝' in operator_guidance(intent, ack)


def test_hil_without_side_explains_rb_gate_and_reconnect_waits_for_ack():
    intent = SimpleNamespace(active_side=None, deadman=False, warning=None)
    ack = SimpleNamespace(accepted=True, reason='powered_hil:hil_latched', receiver_state='HOLD')
    assert '先松 RB' in operator_guidance(intent, ack)
    assert '连接成功不代表恢复运动' in transport_guidance('Connected generation=2. Release controls, select side, then press RB.')

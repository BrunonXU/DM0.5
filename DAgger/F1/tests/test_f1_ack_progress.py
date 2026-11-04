from types import SimpleNamespace as NS

import pytest
from dagger_core.async_transport import AckProgress


def packet(seq, session='f1'):
    return NS(sequence=seq, session_id=session)


def test_missing_old_ack_does_not_disconnect_advancing_stream():
    w = AckProgress(0, .5, 1.)
    w.sent(packet(0), 0)
    for i in range(1, 21):
        now = i*.1
        w.sent(packet(i), now)
        assert w.acknowledge(packet(i), now+.02) == pytest.approx(.02)
        w.check_alive(now+.03)
    assert w.expired_count == 1
    assert w.acknowledge(packet(0), 2.1) is None
    assert w.highest_ack == 20


def test_late_ack_can_advance_but_duplicates_cannot_keep_connection_alive():
    w = AckProgress(0, .5, 1.)
    w.sent(packet(0), 0)
    w.expire(.6)
    assert w.acknowledge(packet(0), .7) == .7
    assert w.acknowledge(packet(0), 1.5) is None
    with pytest.raises(TimeoutError):
        w.check_alive(1.71)


def test_unknown_session_or_future_ack_never_refreshes_liveness():
    w = AckProgress(0, .5, 1.)
    w.sent(packet(3), .1)
    for ack in (packet(3, 'other'), packet(4)):
        with pytest.raises(ConnectionError):
            w.acknowledge(ack, .2)
    assert w.last_progress == 0


def test_history_is_bounded_without_acknowledgements():
    w = AckProgress(0, .5, 1.)
    for i in range(2000):
        w.sent(packet(i), i*.001)
    assert len(w.pending) <= 256 and len(w.expired) <= 256

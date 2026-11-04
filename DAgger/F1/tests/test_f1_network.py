"""Real sockets: an abandoned tunnel must not monopolize the F710 listener."""
import socket
import time
import threading

import pytest

from f1dagger.network import IntentServer
from g2dagger.protocol import IntentPacket, decode_ack, encode_packet


def packet(seq=0):
    return IntentPacket('network-test', seq, time.time_ns(), False, None,
                        'combined', (0., 0., 0.), 0., False)


def test_ack_backpressure_does_not_block_intent_receive(monkeypatch):
    """Block all receiver writes while a newer real socket intent arrives."""
    original_send = socket.socket.send
    allow_ack = threading.Event()
    def blocked_send(sock, data, *args, **kwargs):
        if threading.current_thread().name == 'f1-f710' and not allow_ack.is_set():
            raise BlockingIOError()
        return original_send(sock, data, *args, **kwargs)
    monkeypatch.setattr(socket.socket, 'send', blocked_send)
    server = IntentServer(0)
    try:
        with socket.create_connection(server.sock.getsockname(), timeout=2) as sender:
            sender.sendall(encode_packet(packet(0)))
            sender.sendall(encode_packet(packet(1)))
            deadline = time.monotonic() + .5
            while time.monotonic() < deadline:
                latest, reason = server.snapshot('waiting_for_start')
                if latest is not None and latest[0].sequence == 1:
                    break
                time.sleep(.005)
            assert latest[0].sequence == 1 and reason is None
            allow_ack.set()
            data = b''
            while data.count(b'\n') < 2:
                data += sender.recv(4096)
            assert [decode_ack(line).sequence for line in data.splitlines()] == [0, 1]
    finally:
        allow_ack.set(); server.close()


@pytest.mark.parametrize('prefix', [b'', b'{"session_id":'])
def test_idle_or_incomplete_connection_releases_next_sender(prefix):
    server = IntentServer(0)
    abandoned = socket.create_connection(server.sock.getsockname(), timeout=2)
    try:
        if prefix:
            abandoned.sendall(prefix)
        with socket.create_connection(server.sock.getsockname(), timeout=2) as sender:
            sender.sendall(encode_packet(packet()))
            ack = decode_ack(sender.recv(4096))
            assert ack.accepted and ack.sequence == 0
            latest, reason = server.snapshot('waiting_for_teleop_start')
            assert latest[0].session_id == 'network-test'
            assert reason == 'operator_transport_error:intent packet timeout'
    finally:
        abandoned.close()
        server.close()


def test_continuous_sender_retains_connection_past_idle_deadline():
    server = IntentServer(0)
    try:
        with socket.create_connection(server.sock.getsockname(), timeout=2) as sender:
            for seq in range(13):
                sender.sendall(encode_packet(packet(seq)))
                assert decode_ack(sender.recv(4096)).sequence == seq
                time.sleep(.1)
            assert server.snapshot('waiting_for_teleop_start')[1] is None
    finally:
        server.close()


@pytest.mark.parametrize('mode', ['shadow', 'powered'])
def test_runtime_mode_is_explicit_in_accepted_ack_and_replay_stays_rejected(mode):
    server = IntentServer(0, execution_mode=mode)
    try:
        server.snapshot('hil_latched')
        with socket.create_connection(server.sock.getsockname(), timeout=2) as sender:
            sender.sendall(encode_packet(packet()))
            ack = decode_ack(sender.recv(4096))
            assert ack.accepted and ack.receiver_state == 'HOLD'
            assert ack.reason == 'powered_hil:hil_latched;execution_mode=' + mode
            sender.sendall(encode_packet(packet()))
            ack = decode_ack(sender.recv(4096))
            assert not ack.accepted and ';execution_mode=' not in ack.reason
    finally:
        server.close()

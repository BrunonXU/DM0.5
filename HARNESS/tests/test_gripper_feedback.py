"""Independent sensor evidence must never be substituted by a command echo."""
import socket
import struct
import time

import pytest

from f1_harness.gripper_feedback import PassiveCanGripper, decode_status, kernel_timestamp, SO_TIMESTAMPNS


def packet(raw=255, identifier=0x301, length=8):
    return struct.pack('=IB3x8B', identifier, length, 0, 0, raw, 0, 4, 0, 0, 0)


def decode(data=None, flags=0, **kwargs):
    params = dict(response_id=0x301, source_instance='source-a', sequence=1,
                  received_ns=1000, received_realtime_ns=10000, kernel_received_realtime_ns=9900)
    params.update(kwargs)
    return decode_status(packet() if data is None else data, flags, **params)


@pytest.mark.parametrize('raw,expected', [(255, 0.), (0, 100.), (254, 100. / 255), (73, 18200. / 255)])
def test_mapping_retains_native_resolution_and_separate_units(raw, expected):
    row = decode(packet(raw))
    assert row['raw_position'] == raw
    assert row['closure_percent'] == pytest.approx(expected)
    assert row['velocity_raw'] == 0 and row['current_raw'] == 4
    assert row['time_valid'] and row['queue_delay_ns'] == 100


@pytest.mark.parametrize('data,flags', [
    (packet(identifier=0x201), 0), (packet(identifier=0x300), 0),
    (packet(identifier=0x80000301), 0), (packet(identifier=0x40000301), 0),
    (packet(identifier=0x20000301), 0), (packet(length=5), 0),
    (packet()[:12], 0), (packet() + b'\0' * 8, 0),
    (packet(), socket.MSG_DONTROUTE), (packet(), socket.MSG_TRUNC),
])
def test_wrong_identity_and_command_loopback_are_never_measurements(data, flags):
    assert decode(data, flags) is None


@pytest.mark.parametrize('stamp', [None, 10001])
def test_missing_kernel_time_and_clock_reversal_are_not_fresh(stamp):
    row = decode(kernel_received_realtime_ns=stamp)
    assert row['time_valid'] is False
    assert row['received_ns'] == 1000
    assert row['kernel_received_realtime_ns'] == stamp


def test_queue_delay_is_preserved_instead_of_retimestamping_old_frames():
    row = decode(received_realtime_ns=1_000_009_900)
    assert row['queue_delay_ns'] == 1_000_000_000
    assert row['kernel_received_realtime_ns'] == 9900


def test_actual_can_fd_eight_byte_status_uses_same_measurement_mapping():
    data = bytearray(packet(254) + b'\0' * 56)
    data[5] = 5  # Actual external CAN-FD status: FDF + BRS.
    row = decode(bytes(data))
    assert row['frame_format'] == 'can_fd' and row['can_fd_flags'] == 5
    assert row['closure_percent'] == pytest.approx(100. / 255)
    assert decode(packet())['frame_format'] == 'classic_can'
    data[4] = 12
    assert decode(bytes(data)) is None


def test_kernel_timestamp_parses_only_valid_original_time():
    anc = [(socket.SOL_SOCKET, SO_TIMESTAMPNS, struct.pack('@ll', 123, 456))]
    assert kernel_timestamp(anc) == 123_000_000_456
    assert kernel_timestamp([]) is None
    assert kernel_timestamp([(socket.SOL_SOCKET, SO_TIMESTAMPNS, b'bad')]) is None
    assert kernel_timestamp([(socket.SOL_SOCKET, SO_TIMESTAMPNS, struct.pack('@ll', 123, 10**9))]) is None


class ReceiveOnlySocket:
    """Deliberately has no send method; any transmit attempt fails the test."""
    def __init__(self, frames=()):
        self.frames = list(frames)
        self.closed = False
        self.options = []

    def setsockopt(self, *args): self.options.append(args)
    def settimeout(self, timeout): pass
    def bind(self, address): self.address = address
    def close(self): self.closed = True
    def recvmsg(self, *_):
        if self.closed:
            raise OSError(9, 'closed')
        if not self.frames:
            time.sleep(.001)
            raise TimeoutError()
        data, flags = self.frames.pop(0)
        now = time.time_ns()
        return data, [(socket.SOL_SOCKET, SO_TIMESTAMPNS, struct.pack('@ll', now // 10**9, now % 10**9))], flags, ('can1',)


def test_receive_window_and_lifecycle_never_mix_epoch_or_invent_initial_sample(monkeypatch):
    channel = ReceiveOnlySocket([(packet(), socket.MSG_DONTROUTE)] + [(packet(i), 0) for i in range(70)])
    monkeypatch.setattr(socket, 'socket', lambda *_: channel)
    reader = PassiveCanGripper(interface='can1', response_id=0x301)
    assert reader.snapshot()['latest'] is None
    assert reader.start()
    can_filter = next(v for level, option, v in channel.options
                      if level == socket.SOL_CAN_RAW and option == socket.CAN_RAW_FILTER)
    assert struct.unpack('=II', can_filter) == (0x301, 0xC00007FF)
    assert (socket.SOL_CAN_RAW, socket.CAN_RAW_FD_FRAMES, 1) in channel.options
    deadline = time.monotonic() + 2
    while reader.snapshot()['received_count'] < 70 and time.monotonic() < deadline:
        time.sleep(.001)
    snap = reader.snapshot()
    assert snap['received_count'] == 70 and snap['rejected_count'] == 1
    assert len(snap['samples']) == 64
    assert [row['sequence'] for row in snap['samples']] == list(range(7, 71))
    assert all(row['source_instance'] == reader.source_instance for row in snap['samples'])
    snap['latest']['raw_position'] = -1
    snap['samples'][0]['raw_position'] = -1
    assert reader.snapshot()['latest']['raw_position'] == 69
    assert reader.snapshot()['samples'][0]['raw_position'] == 6
    reader.close()
    assert not reader.snapshot()['running']
    assert channel.closed
    with pytest.raises(RuntimeError, match='source epoch'):
        reader.start()
    replacement = PassiveCanGripper(interface='can1', response_id=0x301)
    assert replacement.source_instance != reader.source_instance
    assert replacement.snapshot()['latest'] is None


def test_unavailable_interface_is_reported_without_fallback_to_command(monkeypatch):
    def unavailable(*_): raise OSError(19, 'missing interface')
    monkeypatch.setattr(socket, 'socket', unavailable)
    reader = PassiveCanGripper(interface='can1', response_id=0x301)
    assert not reader.start()
    snap = reader.snapshot()
    assert not snap['running'] and snap['latest'] is None and not snap['samples']
    assert snap['error'] == 'OSError:19'
    reader.close()

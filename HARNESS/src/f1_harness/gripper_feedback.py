"""Passive OmniPicker measurements, separate from command-filled ROS state.

The installed motor_ctl driver writes commands and CAN measurements into the
same getPosition() field. This reader accepts only external status frames; it
has no transmit operation. The byte layout and mapping match the installed
driver (SHA256 898ef58fb7118ecbf416af134e01700ed38ffdd3480293cf06fe95da14bce0c8).
Velocity/current bytes have no verified physical units and remain diagnostics.
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
import re
import socket
import struct
import threading
import time
from typing import Literal, TypedDict
import uuid


class GripperFeedbackSample(TypedDict):
    source_instance: str
    sequence: int
    received_ns: int  # Host monotonic time of recvmsg; never a device timestamp.
    kernel_received_realtime_ns: int | None
    queue_delay_ns: int | None
    time_valid: bool
    frame_format: Literal['classic_can', 'can_fd']
    can_fd_flags: int | None
    raw_position: int
    closure_percent: float  # 0=open, 100=closed; not degrees or radians.
    velocity_raw: int
    current_raw: int
    data_hex: str


class GripperFeedbackSnapshot(TypedDict):
    schema: Literal['f1-gripper-feedback-v1']
    source: Literal['socketcan_external']
    protocol: Literal['omnipicker_status_v1']
    side: str
    interface: str
    response_id: int
    source_instance: str
    boot_id: str
    clock_domain: Literal['host_monotonic']
    position_unit: Literal['closure_0_open_100_closed']
    position_resolution: float
    running: bool
    error: str | None
    received_count: int
    rejected_count: int
    latest: GripperFeedbackSample | None
    samples: list[GripperFeedbackSample]


CAN_FLAGS = 0xE0000000  # EFF, RTR, ERR; only standard data identifiers accepted.
CAN_DATA_FILTER_FLAGS = 0xC0000000  # ERR in a filter mask selects the error subscription list.
CAN_SFF_MASK = 0x7FF
SO_TIMESTAMPNS = getattr(socket, 'SO_TIMESTAMPNS', 35)


def decode_status(packet: bytes, message_flags: int, *, response_id: int,
                  source_instance: str, sequence: int, received_ns: int,
                  received_realtime_ns: int, kernel_received_realtime_ns: int | None
                  ) -> GripperFeedbackSample | None:
    """Decode one external standard CAN/CAN-FD status without inventing time.

    MSG_DONTROUTE marks frames originating on this host, including another
    socket's commands. Merely disabling reception of our own messages would
    not exclude those. Kernel timestamp absence/clock errors remain explicit;
    callers must not treat such samples as fresh measurement evidence.
    """
    if len(packet) not in (16, 72) or message_flags & (socket.MSG_DONTROUTE | socket.MSG_TRUNC):
        return None
    identifier, length = struct.unpack_from('=IB', packet)
    if identifier & CAN_FLAGS or identifier != response_id or length != 8:
        return None
    data = packet[8:16]
    delay = (received_realtime_ns - kernel_received_realtime_ns
             if kernel_received_realtime_ns is not None else None)
    return GripperFeedbackSample(
        source_instance=source_instance, sequence=sequence, received_ns=received_ns,
        kernel_received_realtime_ns=kernel_received_realtime_ns,
        queue_delay_ns=delay, time_valid=bool(delay is not None and delay >= 0),
        frame_format='can_fd' if len(packet) == 72 else 'classic_can',
        can_fd_flags=packet[5] if len(packet) == 72 else None,
        raw_position=data[2], closure_percent=(255 - data[2]) * 100. / 255,
        velocity_raw=data[3], current_raw=data[4], data_hex=data.hex())


def kernel_timestamp(ancillary: list[tuple[int, int, bytes]]) -> int | None:
    for level, kind, value in ancillary:
        if level == socket.SOL_SOCKET and kind == SO_TIMESTAMPNS and len(value) == struct.calcsize('@ll'):
            sec, nsec = struct.unpack('@ll', value)
            if sec >= 0 and 0 <= nsec < 1_000_000_000:
                return sec * 1_000_000_000 + nsec
    return None


class PassiveCanGripper:
    """One bounded receive-only socket; lifecycle belongs to its consumer.

    Every instance owns a new source identity. Closing it makes its snapshot
    unavailable; restarting requires a new instance, so old/new epochs cannot
    silently share a stability window. No socket or thread exists before start.
    """
    def __init__(self, *, interface: str, response_id: int, side: str = 'right',
                 window_samples: int = 64):
        if not isinstance(interface, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', interface):
            raise ValueError('invalid CAN interface')
        if type(response_id) is not int or not 0 <= response_id <= CAN_SFF_MASK:
            raise ValueError('standard CAN response ID required')
        if side not in ('left', 'right'):
            raise ValueError('left/right side required')
        if type(window_samples) is not int or not 3 <= window_samples <= 1024:
            raise ValueError('bounded measurement window required')
        self.interface, self.response_id, self.side = interface, response_id, side
        self.source_instance = uuid.uuid4().hex
        self.boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self._samples = deque(maxlen=window_samples)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._socket = self._thread = None
        self._started = False
        self._running = False
        self._error = None
        self._received = self._rejected = 0

    def start(self) -> bool:
        if self._started:
            raise RuntimeError('create a new reader for a new source epoch')
        self._started = True
        channel = None
        try:
            channel = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
            channel.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER,
                struct.pack('=II', self.response_id, CAN_SFF_MASK | CAN_DATA_FILTER_FLAGS))
            # The actual right-hand status uses CAN-FD with an eight-byte
            # payload. Accept both kernel frame formats, never truncate FD.
            channel.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FD_FRAMES, 1)
            channel.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_RECV_OWN_MSGS, 0)
            channel.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
            channel.settimeout(.1)
            channel.bind((self.interface,))
        except OSError as exc:
            if channel is not None:
                channel.close()
            self._error = f'{type(exc).__name__}:{exc.errno}'
            return False
        self._socket = channel
        self._running = True
        self._thread = threading.Thread(target=self._receive, name='f1-gripper-measurement', daemon=True)
        self._thread.start()
        return True

    def _receive(self):
        try:
            while not self._stop.is_set():
                try:
                    packet, ancillary, flags, _ = self._socket.recvmsg(72, 128)
                except TimeoutError:
                    continue
                received_ns, realtime_ns = time.monotonic_ns(), time.time_ns()
                sample = decode_status(packet, flags, response_id=self.response_id,
                    source_instance=self.source_instance, sequence=self._received + 1,
                    received_ns=received_ns, received_realtime_ns=realtime_ns,
                    kernel_received_realtime_ns=kernel_timestamp(ancillary))
                with self._lock:
                    if sample is None:
                        self._rejected += 1
                    else:
                        self._received += 1
                        self._samples.append(sample)
        except OSError as exc:
            if not self._stop.is_set():
                with self._lock:
                    self._error = f'{type(exc).__name__}:{exc.errno}'
        finally:
            with self._lock:
                self._running = False

    def snapshot(self) -> GripperFeedbackSnapshot:
        with self._lock:
            samples = [dict(sample) for sample in self._samples]
            return GripperFeedbackSnapshot(
                schema='f1-gripper-feedback-v1', source='socketcan_external',
                protocol='omnipicker_status_v1', side=self.side, interface=self.interface,
                response_id=self.response_id, source_instance=self.source_instance,
                boot_id=self.boot_id, clock_domain='host_monotonic',
                position_unit='closure_0_open_100_closed', position_resolution=100. / 255,
                running=self._running, error=self._error, received_count=self._received,
                rejected_count=self._rejected,
                latest=dict(samples[-1]) if samples else None, samples=samples)

    def close(self):
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
        if self._thread is not None:
            self._thread.join(timeout=.5)
        with self._lock:
            self._running = False

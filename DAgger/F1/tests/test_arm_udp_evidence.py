"""Offline tests: synthetic packets only; no robot, raw socket or SDK connection."""
import importlib.util
import json
from pathlib import Path
import socket
import struct

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


C = load("69_capture_arm_udp_readonly")
D = load("70_decode_arm_udp")


def dcss(sequence=9):
    buf = bytearray(1430)
    buf[:2] = b"FX"
    struct.pack_into("<3i", buf, 14, 3, 3, 0)
    struct.pack_into("<i", buf, 398, 1)
    struct.pack_into("<i", buf, 402, sequence - 1)
    struct.pack_into("<i", buf, 1074, sequence)
    struct.pack_into("<7f", buf, 482, -1, -2, -3, -4, -5, 58.187728, -7)
    struct.pack_into("<7f", buf, 1078, -11, -12, -13, -14, -15, 54.779636, -17)
    struct.pack_into("<7f", buf, 1162, -21, -22, -23, -24, -25, 55.0, -27)
    return bytes(buf)


def packet(payload=b"test", src=D.PEER, dst="192.0.2.5", sport=30000,
           dport=4730, protocol=17, fragment=0, vlan=False):
    udp = struct.pack("!HHHH", sport, dport, len(payload) + 8, 0) + payload
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 1, fragment,
                     64, protocol, 0, socket.inet_aton(src), socket.inet_aton(dst))
    ethernet = b"\0" * 12 + (b"\x81\x00\x00\x01\x08\x00" if vlan else b"\x08\x00")
    return ethernet + ip + udp


@pytest.mark.parametrize("src,dst,sport,dport", [
    (D.PEER, "192.0.2.5", 30000, 4730),
    ("192.0.2.5", D.PEER, 34093, 4729),
    (D.PEER, "192.0.2.5", 4729, 4730),
])
def test_ephemeral_ports_not_excluded(src, dst, sport, dport):
    assert C.matches(packet(src=src, dst=dst, sport=sport, dport=dport))


def test_non_peer_tcp_fragments_truncated_and_vlan():
    assert not C.matches(packet(src="192.0.2.6"))
    assert not C.matches(packet(protocol=6))
    assert not C.matches(packet(fragment=0x2000))
    assert not C.matches(packet()[:-1])
    assert not C.matches(b"\0" * 10)
    assert C.matches(packet(vlan=True))


def test_static_audited_dcss_offsets_and_endianness():
    result = D.decode_dcss(dcss())
    assert result["right_j6_sdk_input_deg"] == pytest.approx(58.187728)
    assert result["right_j6_fb_command_deg"] == 55.0
    assert result["right_j6_fb_position_deg"] == pytest.approx(54.779636)
    assert result["right_output_frame"] == 9
    assert result["right_input_frame"] == 8
    assert result["right_state"] == 3
    assert result["right_impedance_type"] == 1
    assert D.decode_dcss(b"XX" + dcss()[2:]) is None
    assert D.decode_dcss(dcss()[:-1]) is None
    buf = bytearray(dcss()); struct.pack_into("<f", buf, 502, float("nan"))
    assert D.decode_dcss(buf) is None


def test_ring_retains_complete_latest_packets_and_bounded_bytes(tmp_path):
    writer = C.RingWriter(tmp_path, segment_bytes=70000, segment_count=2)
    for i in range(180):
        writer.write(packet(dcss(i + 1)), (i + 1) * 1_000_000_000)
    writer.close()
    info = {"segments": writer.segments()}
    (tmp_path / "capture.json").write_text(json.dumps(info))
    paths = D.capture_paths(tmp_path)
    assert len(paths) == 2
    assert sum(p.stat().st_size for p in paths) <= 140000
    records = [record for p in paths for record in D.pcap_records(p)]
    assert records[-1][0] == 180_000_000_000
    assert records[0][0] > 1_000_000_000
    assert all(D.decode_dcss(D.parse_udp(frame)["payload"]) for _, frame in records)
    assert records[-1][1] == packet(dcss(180))


class FakeSocket:
    def __init__(self, frames):
        self.frames = iter(frames)

    def recv(self, size):
        try:
            return next(self.frames)
        except StopIteration:
            raise KeyboardInterrupt


def test_no_packets_or_plain_udp_is_failure(tmp_path, capsys):
    with pytest.raises(SystemExit, match="采证失败"):
        C.capture_loop(FakeSocket([packet()]), tmp_path, 600)
    result = json.loads((tmp_path / "capture.json").read_text())
    assert result["status"] == "failed_no_valid_feedback"
    assert result["valid_dcss_packets"] == 0
    assert "采证有效" not in capsys.readouterr().out


def test_wait_beyond_two_minutes_then_valid_feedback(monkeypatch, tmp_path, capsys):
    times = iter(range(0, 1000, 15))
    monkeypatch.setattr(C.time, "monotonic", lambda: next(times))
    result = C.capture_loop(FakeSocket([packet()] * 6 + [packet(dcss(10)), packet(dcss(11))]), tmp_path, 600)
    assert result["stop_reason"] == "operator_interrupt"
    assert result["valid_dcss_packets"] == 2
    assert result["status"] == "saved_valid_feedback"
    assert "采证有效" in capsys.readouterr().out
    assert result["retained_packets"] == 8


def test_decoder_rejects_24_byte_empty_capture(tmp_path, monkeypatch):
    pcap = tmp_path / "empty.pcap"
    pcap.write_bytes(C.PCAP_HEADER)
    monkeypatch.setattr("sys.argv", ["decode", str(pcap), "--output", str(tmp_path / "empty.csv")])
    with pytest.raises(SystemExit, match="No valid"):
        D.main()


def test_io_failure_after_valid_feedback_does_not_report_success(tmp_path):
    class BrokenSocket(FakeSocket):
        def recv(self, size):
            try:
                return next(self.frames)
            except StopIteration:
                raise OSError("synthetic socket failure")
    with pytest.raises(OSError, match="synthetic"):
        C.capture_loop(BrokenSocket([packet(dcss(10)), packet(dcss(11))]), tmp_path, 600)
    result = json.loads((tmp_path / "capture.json").read_text())
    assert result["status"] == "failed_capture_error"
    assert result["stop_reason"] == "capture_error"
    assert result["valid_dcss_packets"] == 2
    assert result["retained_packets"] == 2


def test_decoder_roundtrip_csv_and_preserve_layer_meaning(tmp_path, monkeypatch):
    pcap = tmp_path / "sample.pcap"
    pcap.write_bytes(C.PCAP_HEADER + C.packet_record(packet(dcss()), 1234567890))
    output = tmp_path / "sample.csv"
    monkeypatch.setattr("sys.argv", ["decode", str(pcap), "--output", str(output)])
    D.main()
    text = output.read_text()
    assert "right_j6_sdk_input_deg" in text
    assert "right_j6_fb_command_deg" in text
    assert "right_j6_fb_position_deg" in text
    assert "1234567000" in text  # pcap microsecond timestamp precision
    with pytest.raises(FileExistsError):
        D.main()

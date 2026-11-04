#!/usr/bin/env python3
"""Offline decoder for the audited MarvinSDK FX/DCSS feedback; never opens a socket.

Evidence: libMarvinSDK.so SHA256 8580298c967ba2ca9166a7175a289a59c3d5aa3a9ae09c17373fde36c4092346.
CRobot::DoRecv 0x19e10 checks 1430 bytes; 0x19e18..24 checks FX;
0x19e38..44 copies payload[2:1430] into DCSS without byte swapping.
FxType.h + FxRtCSDef.h pack(4): StateCtr=12, RT_IN=368, RT_OUT=312, DCSS=1428.
Offsets below are UDP payload offsets, NOT Ethernet/IP offsets. Degrees follow
existing F1 SDK/ROS contract; a new firmware requires a new schema audit.
"""
import argparse
import csv
import json
import math
from pathlib import Path
import socket
import struct

SCHEMA = "marvin-fx-dcss-1428-le-pack4-audit-20261015"
PEER = "192.0.2.4"


def parse_udp(frame):
    if len(frame) < 14:
        return None
    protocol = struct.unpack_from("!H", frame, 12)[0]
    start = 14
    for _ in range(2):
        if protocol not in (0x8100, 0x88A8):
            break
        if len(frame) < start + 4:
            return None
        protocol = struct.unpack_from("!H", frame, start + 2)[0]
        start += 4
    if protocol != 0x0800 or len(frame) < start + 20:
        return None
    ihl = (frame[start] & 15) * 4
    if frame[start] >> 4 != 4 or ihl < 20 or len(frame) < start + ihl + 8:
        return None
    length, fragments = struct.unpack_from("!H", frame, start + 2)[0], struct.unpack_from("!H", frame, start + 6)[0]
    if frame[start + 9] != 17 or fragments & 0x3FFF or length < ihl + 8 or len(frame) < start + length:
        return None
    src, dst = socket.inet_ntoa(frame[start + 12:start + 16]), socket.inet_ntoa(frame[start + 16:start + 20])
    sport, dport, size = struct.unpack_from("!HHH", frame, start + ihl)
    if size < 8 or size > length - ihl:
        return None
    return {"src": src, "dst": dst, "sport": sport, "dport": dport,
            "payload": frame[start + ihl + 8:start + ihl + size]}


def decode_dcss(payload):
    if len(payload) != 1430 or payload[:2] != b"FX":
        return None
    arrays = {name: struct.unpack_from("<7f", payload, offset) for name, offset in
              (("sdk_input_deg", 482), ("fb_position_deg", 1078), ("fb_command_deg", 1162))}
    if not all(math.isfinite(v) for values in arrays.values() for v in values):
        return None
    incoming = struct.unpack_from("<i", payload, 402)[0]
    outgoing = struct.unpack_from("<i", payload, 1074)[0]
    # FxRtCSDef.h documents both serials as "0 - 1000000 modulo";
    # DoRecv also computes modulo 1000000 at 0x19e88..0x19eac.
    if not (0 <= incoming <= 1_000_000 and 0 <= outgoing <= 1_000_000):
        return None
    current_state, command_state, error_code = struct.unpack_from("<3i", payload, 14)
    return {"schema": SCHEMA, "units": "deg", "right_input_frame": incoming,
            "right_output_frame": outgoing, "right_state": current_state,
            "right_command_state": command_state, "right_error_code": error_code,
            "right_impedance_type": struct.unpack_from("<i", payload, 398)[0],
            **{"right_j6_" + name: values[5] for name, values in arrays.items()}}


def pcap_records(path):
    with Path(path).open("rb") as stream:
        header = stream.read(24)
        if len(header) != 24:
            raise ValueError(f"{path}: truncated pcap header")
        versions = {b"\xd4\xc3\xb2\xa1": ("<", 1000), b"\xa1\xb2\xc3\xd4": (">", 1000),
                    b"\x4d\x3c\xb2\xa1": ("<", 1), b"\xa1\xb2\x3c\x4d": (">", 1)}
        if header[:4] not in versions:
            raise ValueError(f"{path}: unsupported pcap format")
        endian, nanos = versions[header[:4]]
        if struct.unpack_from(endian + "I", header, 20)[0] != 1:
            raise ValueError(f"{path}: expected Ethernet link type")
        while True:
            record = stream.read(16)
            if not record:
                break
            if len(record) != 16:
                raise ValueError(f"{path}: partial record header")
            seconds, fraction, size, original = struct.unpack(endian + "IIII", record)
            if size > 65535 or original < size:
                raise ValueError(f"{path}: invalid packet length")
            frame = stream.read(size)
            if len(frame) != size:
                raise ValueError(f"{path}: partial packet")
            yield seconds * 1_000_000_000 + fraction * nanos, frame


def capture_paths(path):
    path = Path(path)
    if path.is_file():
        return [path]
    info = json.loads((path / "capture.json").read_text())
    if "segments" not in info:
        return [path / "arm.pcap"]
    output = []
    for row in sorted(info["segments"], key=lambda row: row["generation"]):
        name = row["name"]
        if Path(name).name != name:
            raise ValueError("invalid capture segment name")
        output.append(path / name)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="New CSV path; never overwrite")
    args = parser.parse_args()
    rows = 0
    packets = 0
    with args.output.open("x", newline="") as output:
        writer = None
        for path in capture_paths(args.capture):
            for timestamp, frame in pcap_records(path):
                packets += 1
                udp = parse_udp(frame)
                if not udp or udp["src"] != PEER:
                    continue
                decoded = decode_dcss(udp["payload"])
                if not decoded:
                    continue
                row = {"capture_wall_ns": timestamp, "source_file": path.name,
                       **{k: udp[k] for k in ("src", "dst", "sport", "dport")}, **decoded}
                if writer is None:
                    writer = csv.DictWriter(output, fieldnames=list(row))
                    writer.writeheader()
                writer.writerow(row)
                rows += 1
    report = {"packets": packets, "dcss_rows": rows, "schema": SCHEMA,
              "capture_timestamps_are_userspace_receipt": True,
              "sdk_input_is_controller_echo_not_motor_arrival": True}
    print(json.dumps(report, indent=2))
    if not rows:
        raise SystemExit("No valid FX/DCSS feedback decoded; this is not successful diagnostic evidence")


if __name__ == "__main__":
    main()

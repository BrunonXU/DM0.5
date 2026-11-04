#!/usr/bin/env python3
"""Passive F1 arm UDP ring capture. No SDK, transmitted frames, or promiscuous mode."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import socket
import struct
import time

INTERFACE = "enP3p1s0f2"
PEER = "192.0.2.4"
SEGMENT_BYTES = 16 * 1024 * 1024
SEGMENT_COUNT = 8
PCAP_HEADER = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
_spec = importlib.util.spec_from_file_location("arm_udp_decoder", Path(__file__).with_name("70_decode_arm_udp.py"))
_decoder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_decoder)
parse_udp, decode_dcss = _decoder.parse_udp, _decoder.decode_dcss


def matches(frame):
    packet = parse_udp(frame)
    return bool(packet and PEER in (packet["src"], packet["dst"]))


def packet_record(frame, timestamp_ns):
    seconds, ns = divmod(timestamp_ns, 1_000_000_000)
    return struct.pack("<IIII", seconds, ns // 1000, len(frame), len(frame)) + frame


class RingWriter:
    def __init__(self, directory, segment_bytes=SEGMENT_BYTES, segment_count=SEGMENT_COUNT):
        if segment_bytes < 24 + 16 + 65535 or segment_count < 2:
            raise ValueError("capture ring bounds too small")
        self.directory, self.limit, self.count = directory, segment_bytes, segment_count
        self.generation, self.output, self.rows = -1, None, {}
        self.rotate()

    def rotate(self):
        if self.output:
            self.output.close()
        self.generation += 1
        slot = self.generation % self.count
        name = f"arm-{slot:02d}.pcap"
        self.output = (self.directory / name).open("wb")
        self.output.write(PCAP_HEADER)
        self.current = {"name": name, "generation": self.generation, "bytes": 24,
                        "packets": 0, "first_wall_ns": None, "last_wall_ns": None}
        self.rows[slot] = self.current

    def write(self, frame, timestamp_ns):
        record = packet_record(frame, timestamp_ns)
        if self.current["bytes"] + len(record) > self.limit:
            self.rotate()
        self.output.write(record)
        self.current["bytes"] += len(record)
        self.current["packets"] += 1
        if self.current["first_wall_ns"] is None:
            self.current["first_wall_ns"] = timestamp_ns
        self.current["last_wall_ns"] = timestamp_ns

    def segments(self):
        if self.output and not self.output.closed:
            self.output.flush()
        return sorted(self.rows.values(), key=lambda row: row["generation"])

    def close(self):
        self.output.close()


def save_info(directory, info, writer):
    info["segments"] = writer.segments()
    info["retained_packets"] = sum(r["packets"] for r in info["segments"])
    info["retained_pcap_bytes"] = sum(r["bytes"] for r in info["segments"])
    temporary = directory / "capture.json.tmp"
    temporary.write_text(json.dumps(info, indent=2) + "\n")
    temporary.replace(directory / "capture.json")


def capture_loop(capture, directory, seconds):
    started = time.monotonic()
    info = {"schema": _decoder.SCHEMA, "interface": INTERFACE, "peer": PEER,
            "udp_port_filter": None, "duration_limit_s": seconds,
            "ring_bytes_limit": SEGMENT_BYTES * SEGMENT_COUNT,
            "start_wall_ns": time.time_ns(), "start_monotonic_ns": time.monotonic_ns(),
            "timestamp_source": "userspace wall clock immediately after recv; not controller time",
            "sdk_connection_created": False, "promiscuous_mode_requested": False, "frames_sent": 0,
            "status": "waiting_for_valid_feedback", "matched_udp_packets": 0,
            "valid_dcss_packets": 0, "outgoing_udp_packets": 0, "incoming_udp_packets": 0,
            "endpoints": {}, "stop_reason": None}
    writer = RingWriter(directory)
    last_status = started
    first_frame, verified = None, False
    print(f"监听已打开，等待有效双臂反馈；尚未完成采证检查。按 Ctrl+C 结束，仅监听不控制。最长 {seconds:g} 秒。", flush=True)
    save_info(directory, info, writer)
    try:
        while time.monotonic() - started < seconds:
            try:
                frame = capture.recv(65535)
                received = time.time_ns()
            except socket.timeout:
                frame = None
            if frame is not None:
                udp = parse_udp(frame)
                if udp and PEER in (udp["src"], udp["dst"]):
                    writer.write(frame, received)
                    info["matched_udp_packets"] += 1
                    direction = "incoming" if udp["src"] == PEER else "outgoing"
                    info[direction + "_udp_packets"] += 1
                    endpoint = f'{udp["src"]}:{udp["sport"]}>{udp["dst"]}:{udp["dport"]}'
                    if len(info["endpoints"]) < 32 or endpoint in info["endpoints"]:
                        info["endpoints"][endpoint] = info["endpoints"].get(endpoint, 0) + 1
                    feedback = decode_dcss(udp["payload"]) if direction == "incoming" else None
                    if feedback:
                        info["valid_dcss_packets"] += 1
                        info["last_feedback"] = {"received_wall_ns": received, **feedback}
                        sequence = feedback["right_output_frame"]
                        if first_frame is None:
                            first_frame = sequence
                        elif sequence != first_frame and not verified:
                            verified = True
                            info["status"] = "valid_feedback_received"
                            print("采证有效：已收到连续更新的双臂反馈。请按现场安排通过原入口操作 Y；操作后在本终端按 Ctrl+C 保存。", flush=True)
            now = time.monotonic()
            if now - last_status >= 5:
                save_info(directory, info, writer)
                if not verified:
                    print(f'尚未收到连续有效反馈：peer UDP {info["matched_udp_packets"]} 包 / FX {info["valid_dcss_packets"]} 包；此时不要把操作当成已取证。', flush=True)
                else:
                    age_s = (time.time_ns() - info["last_feedback"]["received_wall_ns"]) / 1e9
                    print(f'仍在采证：FX {info["valid_dcss_packets"]} 包，最近反馈 {age_s:.1f}s，剩余上限 {max(0, seconds - (now-started)):.0f}s；操作后 Ctrl+C。', flush=True)
                last_status = now
        info["stop_reason"] = "duration_limit"
    except KeyboardInterrupt:
        info["stop_reason"] = "operator_interrupt"
    except Exception as exc:
        info.update(stop_reason="capture_error", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        writer.close()
        status = "saved_valid_feedback" if verified else "failed_no_valid_feedback"
        if info["stop_reason"] == "capture_error":
            status = "failed_capture_error"
        info.update(end_wall_ns=time.time_ns(), end_monotonic_ns=time.monotonic_ns(),
                    status=status)
        save_info(directory, info, writer)
    if not verified:
        raise SystemExit("采证失败：没有收到连续有效 FX/DCSS 反馈。空文件或普通 UDP 不算成功，请保留 capture.json 排查。")
    print(f"已保存采证：{directory}；仅保留环形文件中的最近报文，实际覆盖区间见 capture.json。", flush=True)
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="New evidence directory; must not exist")
    parser.add_argument("--seconds", type=float, default=600, help="Manual Ctrl+C stop; safety cap 1..1800 seconds, default 600")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 1800:
        parser.error("--seconds must be between 1 and 1800")
    if os.geteuid() != 0:
        parser.error("Run locally on F1 with sudo; passive packet capture requires root")
    socket.if_nametoindex(INTERFACE)
    # ETH_P_ALL also observes outbound packets. No PACKET_MR_PROMISC and no send.
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003)) as capture:
        capture.bind((INTERFACE, 0))
        capture.settimeout(0.2)
        sudo_uid, sudo_gid = int(os.environ.get("SUDO_UID", "0")), int(os.environ.get("SUDO_GID", "0"))
        if sudo_uid > 0:
            os.setgroups([])
            os.setgid(sudo_gid)
            os.setuid(sudo_uid)
        args.out.mkdir(parents=True, exist_ok=False)
        capture_loop(capture, args.out, args.seconds)


if __name__ == "__main__":
    main()

from g2dagger.hil_network_shadow import HILNetworkShadow
from g2dagger.protocol import IntentPacket


def packet(sequence, toggle=0, rb=False, grip=None, session="s"):
    return IntentPacket(session_id=session, sequence=sequence, sent_at_unix_ns=1,
        deadman=rb, active_side="left", mode="combined", axes=(0., 0., 0.),
        grip_velocity=0., thumb_clearance=False, hil_toggle_id=toggle, grip_target=grip)


def test_network_shadow_start_rb_fresh_handoff():
    events = []
    engine = HILNetworkShadow(lambda event, **f: events.append((event, f)), response_delay_ns=10)
    engine.receive(packet(0), 0)
    engine.receive(packet(1, rb=True), 1)
    assert engine.supervisor.state.value == "policy"
    engine.receive(packet(2, 1, True), 2)
    engine.receive(packet(3, 1), 3)
    assert engine.supervisor.state.value == "hil_latched"
    engine.receive(packet(4, 2, True), 4)
    assert engine.supervisor.state.value == "hil_latched"
    engine.receive(packet(5, 3), 5)
    engine.tick(6)
    assert engine.supervisor.state.value == "handoff_hold"
    engine.tick(16)
    assert engine.supervisor.state.value == "policy"
    request = next(f for e, f in events if e == "synthetic_policy_request")
    assert request["monotonic_ns"] > request["handoff_monotonic_ns"]


def test_watchdog_and_reconnect_do_not_resume_policy():
    engine = HILNetworkShadow(lambda *a, **k: None, watchdog_ns=100)
    engine.receive(packet(0), 0)
    engine.tick(101)
    assert engine.supervisor.state.value == "safe_hold"
    engine.receive(packet(0, session="new"), 102)
    assert engine.supervisor.state.value == "safe_hold"
    engine.receive(packet(1, 1, session="new"), 103)
    assert engine.supervisor.state.value == "hil_latched"


def test_rt_without_rb_blocks_exit_until_simulated_completion():
    engine = HILNetworkShadow(lambda *a, **k: None, watchdog_ns=2_000_000_000)
    engine.receive(packet(0), 0)
    engine.receive(packet(1, 1), 1)
    engine.receive(packet(2, 1, grip=1.), 2)
    engine.receive(packet(3, 2, grip=1.), 3)
    assert engine.supervisor.state.value == "hil_latched"
    engine.tick(1_000_000_003)
    engine.receive(packet(4, 3, grip=1.), 1_000_000_004)
    assert engine.supervisor.state.value == "handoff_hold"


def test_late_synthetic_response_rejected_after_new_start():
    events = []
    engine = HILNetworkShadow(lambda event, **f: events.append((event, f)), response_delay_ns=10)
    for seq in range(3):
        engine.receive(packet(seq, seq), seq)
    engine.tick(3)
    engine.receive(packet(3, 3), 4)
    engine.tick(13)
    assert engine.supervisor.state.value == "hil_latched"
    assert any(e == "policy_chunk_rejected" for e, _ in events)


def test_real_socket_mapper_protocol_and_receiver(tmp_path):
    import json
    from pathlib import Path
    import runpy
    import signal
    import socket
    import subprocess
    import sys
    import time
    from g2dagger.f710_mapping import F710Mapper, GamepadSnapshot
    from g2dagger.protocol import encode_packet, decode_ack

    root = Path(__file__).parents[1]
    make_packet = runpy.run_path(str(root / "scripts/03_windows_shadow_sender.py"))["make_packet"]
    mapper = F710Mapper()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    log = tmp_path / "network.jsonl"
    with (tmp_path / "console.txt").open("w") as console:
        process = subprocess.Popen([sys.executable, str(root / "scripts/37_g2_hil_network_shadow.py"),
            "--port", str(port), "--log", str(log)], stdout=console, stderr=console)
        connection = None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    connection = socket.create_connection(("127.0.0.1", port), timeout=.5)
                    break
                except OSError:
                    time.sleep(.02)
            assert connection is not None
            sequence = 0
            def send(sample):
                nonlocal sequence
                intent = mapper.map(sample)
                connection.sendall(encode_packet(make_packet("integration", sequence, intent)))
                data = b""
                while not data.endswith(b"\n"):
                    block = connection.recv(4096)
                    assert block
                    data += block
                assert decode_ack(data).accepted
                sequence += 1
            for sample in (GamepadSnapshot(), GamepadSnapshot(start=True),
                           GamepadSnapshot(), GamepadSnapshot(start=True), GamepadSnapshot()):
                send(sample)
                time.sleep(.02)
            deadline = time.monotonic() + 1.2
            while time.monotonic() < deadline:
                send(GamepadSnapshot())
                time.sleep(.02)
        finally:
            if connection:
                connection.close()
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        assert process.returncode == 0
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    names = [r["event"] for r in rows]
    assert "policy_to_hil" in names and "hil_to_handoff_hold" in names and "fresh_policy_to_policy" in names
    assert all(r["hardware_output"] is False and r["training_ready"] is False for r in rows)

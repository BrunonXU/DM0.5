"""F1 historical-input host comparison; no robot imports or command publishers."""
import argparse
import fcntl
import hashlib
import json
import multiprocessing
from pathlib import Path
import time
import uuid
from urllib.request import urlopen

import numpy as np
from dagger_core.recording_observation import TimestampedFrame, DeviceTimestamp
from f1dagger.rtc_pixels import PixelEncoder
from f1dagger.websocket_policy import WebSocketPolicy, encode_request, network_worker


def stats(values):
    return dict(zip(("p50", "p95", "p99", "max"),
                    np.percentile(values, [50, 95, 99, 100]).tolist()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--pixels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=300)
    parser.add_argument("--mode", choices=("echo", "rtc"), default="rtc")
    args = parser.parse_args()
    lock = open("/home/user/Bruno/F1/runtime/control.lock", "a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    args.output.mkdir(parents=True, exist_ok=False)
    ports = [8136, 8137]
    health = [json.load(urlopen(f"http://127.0.0.1:{port}/health", timeout=5)) for port in ports]
    for key in ("model_id", "model_version", "protocol", "input_preprocessing",
                "denoise_steps", "prediction_horizon"):
        assert health[0][key] == health[1][key], key
    encoder = PixelEncoder(args.pixels, health[0]["input_preprocessing"])
    inputs = json.loads(args.inputs.read_text())
    observations = {r["observation_id"]: r for r in
                    map(json.loads, (args.run / "payload/frames.jsonl").open())}
    samples = []
    for entry in inputs:
        row = observations[entry["observation_id"]]
        assert np.allclose(row["state16"], entry["prediction_replay"]["state16"], atol=0, rtol=0)
        frames = []
        for frame in row["frames"]:
            with (args.run / "payload" / frame["path"]).open("rb") as stream:
                stream.seek(frame["payload_offset"])
                data = stream.read(frame["payload_bytes"])
            assert hashlib.sha256(data).hexdigest() == frame["payload_sha256"]
            frames.append(TimestampedFrame(frame["camera"], frame["frame_id"],
                DeviceTimestamp(**frame["timestamp"]), frame["host_received_monotonic_ns"],
                data, frame["media_type"], frame["width"], frame["height"]))
        samples.append((entry, row, frames))
    clients = []
    context = multiprocessing.get_context("spawn")
    route = "/echo" if args.mode == "echo" else "/ws"
    session = uuid.uuid4().hex
    records = []
    try:
        for port in ports:
            client = WebSocketPolicy(
                {"endpoint": f"http://127.0.0.1:{port}", "timeout_s": 10}, "",
                lambda _: None, held_getter=lambda: np.zeros(16))
            client.pipe, child = context.Pipe()
            client.process = context.Process(target=network_worker, args=(
                child, f"ws://127.0.0.1:{port}{route}", 10), daemon=True)
            client.process.start()
            child.close()
            clients.append(client)
        for pair in range(args.count):
            entry, row, frames = samples[pair % len(samples)]
            meta = dict(contract="f1_upper_16d_v1", protocol="f1_eef_rtc_v2",
                session_id=session, request_id=1000+pair, observation_id=row["observation_id"],
                policy_epoch=0, observation_monotonic_ns=row["capture_started_monotonic_ns"],
                state16=entry["prediction_replay"]["state16"],
                last_submitted16=entry["prediction_replay"]["last_submitted16"],
                state_timestamps_ns=row["state_timestamps_ns"], prediction_horizon=50,
                execute_steps=20, chunk_steps=20, frequency_hz=30., shadow_only=True,
                model_id=health[0]["model_id"], model_version=health[0]["model_version"],
                prepared_pixels=encoder.identity, rtc=entry["rtc_request"])
            hashes = []
            for index in ((0, 1) if pair % 2 == 0 else (1, 0)):
                started = time.perf_counter()
                prepared = encoder.encode(frames)
                payload = encode_request(meta, prepared)
                packed = time.perf_counter()
                result = clients[index]._exchange(payload)
                assert result.get("session_id") == session and result.get("request_id") == 1000+pair
                assert "error" not in result, result
                if result.get("success") and args.mode == "rtc":
                    actions = np.asarray(result["actions"])
                    assert actions.shape == (entry["rtc_request"]["coverage_steps"], 16)
                    assert np.isfinite(actions).all()
                    committed = entry["rtc_request"]["committed_actions16"]
                    if committed:
                        assert np.array_equal(actions[:len(committed)], np.asarray(committed))
                validated = time.monotonic_ns()
                total = (time.perf_counter()-started)*1000
                digest = hashlib.sha256(payload).hexdigest()
                hashes.append(digest)
                stages = result["client_transport_stages"]
                server = result["timing_ms"]
                record = dict(pair=pair, host=("6000D", "H20")[index], port=ports[index],
                    request_id=1000+pair, session_id=session, payload_sha256=digest,
                    payload_bytes=len(payload), bootstrap=entry["rtc_request"]["bootstrap"],
                    success=result["success"], pack_ms=(packed-started)*1000,
                    client_total_ms=total, network_rtt_ms=result["client_network_rtt_ms"],
                    server=server, transport_stages=stages,
                    client_validation_ms=(validated-stages["parent_decoded_ns"])/1e6,
                    rtt_minus_worker_ms=result["client_network_rtt_ms"]-server["worker_total"])
                records.append(record)
                with (args.output / "requests.jsonl").open("a") as stream:
                    stream.write(json.dumps(record)+"\n")
            assert len(set(hashes)) == 1, "Paired payloads differ"
            if pair % 25 == 0:
                print(json.dumps({"pairs_completed": pair+1}), flush=True)
    finally:
        for client in clients:
            client.close()
    summary = dict(mode=args.mode, pairs=args.count, warmup_excluded_pairs=10,
        source_run=str(args.run), health=health, robot_commands=0, hosts={},
        limitations=["Historical replay: no live camera/recording/control load.",
                     "Identical input/prefix; server random noise not synchronized.",
                     "H20 GPU0 exclusive; 6000D GPU2 shared with other training.",
                     "Different torch/python versions; deployment comparison, not hardware-only."])
    for host in ("6000D", "H20"):
        rows = [r for r in records if r["host"] == host and r["pair"] >= 10]
        metrics = {key: stats([r[key] for r in rows]) for key in (
            "pack_ms", "client_total_ms", "network_rtt_ms",
            "client_validation_ms", "rtt_minus_worker_ms")}
        for key in rows[0]["server"]:
            metrics["server_"+key] = stats([r["server"][key] for r in rows])
        for key, begin, end in (
            ("parent_send", "parent_send_started_ns", "parent_send_completed_ns"),
            ("send_to_child", "parent_send_started_ns", "child_received_ns"),
            ("child_reply_to_parent", "child_reply_started_ns", "parent_received_ns"),
            ("parent_decode", "parent_received_ns", "parent_decoded_ns"),
            ("child_decode", "ws_received_ns", "child_decoded_ns")):
            metrics[key+"_ms"] = stats([
                (r["transport_stages"][end]-r["transport_stages"][begin])/1e6 for r in rows])
        summary["hosts"][host] = dict(requests=len(rows), metrics_ms=metrics,
            ik_success=sum(bool(r["success"]) for r in rows),
            over_666ms=sum(r["client_total_ms"] > 1000*20/30 for r in rows))
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()

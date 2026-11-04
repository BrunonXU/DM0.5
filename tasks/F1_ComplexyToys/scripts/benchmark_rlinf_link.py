#!/usr/bin/env python3
"""Historical F1 image transport to the existing /echo endpoint, never /ws.

No ROS, live observations, model inference, or robot command publisher is used.
The control lock is held to ensure a live run cannot overlap this benchmark.
"""
import argparse
import csv
from dataclasses import replace
import fcntl
import hashlib
import json
from io import BytesIO
import multiprocessing
from pathlib import Path
import sys
import time
import uuid
from urllib.request import urlopen


def stats(values):
    import numpy as np
    return dict(zip(("p50", "p95", "p99", "max"),
                    np.percentile(values, [50, 95, 99, 100]).tolist())) if values else {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--model", default="v1_dagger_rightonly_1000")
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=240)
    parser.add_argument("--interval", type=float, default=.15)
    parser.add_argument("--alternate-masked-left", action="store_true",
                        help="Paired right-only transport candidate; inactive left image is fixed black PNG")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    catalog = json.loads(args.catalog.read_text())
    model = catalog["models"][args.model]
    client = Path(catalog["modes"]["rollout"]["client"])
    sys.path[:0] = [str(client / "src"), catalog["robot"]["pythonpath"]]
    from dagger_core.recording_observation import TimestampedFrame, DeviceTimestamp
    from f1dagger.rtc_pixels import PixelEncoder
    from f1dagger.websocket_policy import WebSocketPolicy, network_worker, encode_request
    import f1dagger.rtc_pixels as pixels_module
    import f1dagger.websocket_policy as websocket_module
    lock = open(catalog["robot"]["control_lock"], "r+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    endpoint = f'http://127.0.0.1:{model["local_port"]}'
    health = json.load(urlopen(endpoint + "/health", timeout=5))
    assert (health["model_id"], health["model_version"]) == (model["model_id"], model["model_version"])
    workers = catalog["contracts"][model["contract"]]["config"]["policy"]["rtc_pixel_workers"]
    encoder = PixelEncoder(model["pixels"], health["input_preprocessing"], workers=workers)
    black_png = None
    if args.alternate_masked_left:
        assert model["contract"] == "v1_dagger_rightonly_eef"
        assert encoder.contract["active_cameras"] == ["head", "right_wrist"]
        assert args.count % 2 == 0
        from PIL import Image
        buf = BytesIO()
        Image.new("RGB", (224, 224), (0, 0, 0)).save(buf, format="PNG", compress_level=1)
        black_png = buf.getvalue()
    inputs = json.loads(args.inputs.read_text())
    observations = {r["observation_id"]: r for r in
                    map(json.loads, (args.source_run / "payload/frames.jsonl").open())}
    samples = []
    for entry in inputs:
        row = observations[entry["observation_id"]]
        assert row["state16"] == entry["state16"]
        frames = []
        for frame in row["frames"]:
            assert frame["path"] == frame["camera"] + ".pack"
            with (args.source_run / "payload" / frame["path"]).open("rb") as stream:
                stream.seek(frame["payload_offset"])
                data = stream.read(frame["payload_bytes"])
            assert hashlib.sha256(data).hexdigest() == frame["payload_sha256"]
            frames.append(TimestampedFrame(frame["camera"], frame["frame_id"],
                DeviceTimestamp(**frame["timestamp"]), frame["host_received_monotonic_ns"],
                data, frame["media_type"], frame["width"], frame["height"]))
        samples.append((entry, row, frames))
    session = "rlinf-offline-echo-" + uuid.uuid4().hex
    context = multiprocessing.get_context("spawn")
    transport = WebSocketPolicy({"endpoint": endpoint, "timeout_s": 5}, "",
                                lambda _: None, held_getter=lambda: None)
    transport.pipe, child = context.Pipe()
    transport.process = context.Process(target=network_worker,
        args=(child, endpoint.replace("http://", "ws://") + "/echo", 5), daemon=True)
    transport.process.start()
    child.close()
    records = []
    paired_active_pixels = {}
    started_wall_ns = time.time_ns()
    try:
        for index in range(args.count):
            started = time.perf_counter()
            pair = index // 2 if args.alternate_masked_left else index
            masked = args.alternate_masked_left and (index % 2 != pair % 2)
            variant = "png1_masked_left_blank" if masked else "png1"
            entry, row, frames = samples[pair % len(samples)]
            request_id = index + 10000
            rtc = dict(entry["rtc"])
            rtc["base_plan_id"] = 0 if rtc["bootstrap"] else request_id - 1
            metadata = dict(contract="f1_upper_16d_v1", protocol="f1_eef_rtc_v2",
                session_id=session, request_id=request_id, observation_id=row["observation_id"],
                policy_epoch=0, observation_monotonic_ns=row["capture_started_monotonic_ns"],
                state16=entry["state16"], last_submitted16=entry["last_submitted16"],
                state_timestamps_ns=row["state_timestamps_ns"], prediction_horizon=50,
                execute_steps=20, chunk_steps=20, frequency_hz=30., shadow_only=True,
                model_id=health["model_id"], model_version=health["model_version"],
                prepared_pixels=encoder.identity, rtc=rtc)
            if row.get("state_valid_mask") is not None:
                metadata["state_valid_mask"] = row["state_valid_mask"]
                metadata["absent_state_semantics"] = row["absent_state_semantics"]
            if masked:
                active = {f.camera: f for f in encoder.encode([f for f in frames if f.camera != "left_wrist"])}
                prepared = tuple(replace(f, payload=black_png, media_type="image/png", width=224, height=224)
                                 if f.camera == "left_wrist" else active[f.camera] for f in frames)
            else:
                prepared = encoder.encode(frames)
            encoded_at = time.perf_counter()
            payload = encode_request(metadata, prepared)
            packed_at = time.perf_counter()
            result = transport._exchange(payload)
            finished = time.perf_counter()
            valid = result.get("echo") is True and result.get("success") is True and all(
                result.get(key) == metadata[key] for key in
                ("model_id", "model_version", "session_id", "request_id", "observation_id"))
            record = dict(index=index, pair=pair, variant=variant, inactive_placeholder_cameras=["left_wrist"] if masked else [],
                request_id=request_id, observation_id=row["observation_id"],
                success=valid, error=result.get("error"), bootstrap=rtc["bootstrap"],
                payload_bytes=len(payload), image_bytes=sum(len(f.payload) for f in prepared),
                payload_sha256=hashlib.sha256(payload).hexdigest(),
                pixel_preprocess_png_ms=(encoded_at-started)*1000,
                envelope_ms=(packed_at-encoded_at)*1000, pack_ms=(packed_at-started)*1000,
                client_total_ms=(finished-started)*1000,
                network_rtt_ms=result.get("client_network_rtt_ms"),
                server=result.get("timing_ms"), transport_stages=result.get("client_transport_stages"))
            if valid:
                record["rtt_minus_handler_ms"] = record["network_rtt_ms"]-record["server"]["handler"]
                record["rtt_minus_worker_ms"] = record["network_rtt_ms"]-record["server"]["worker_total"]
            records.append(record)
            if args.alternate_masked_left:
                digest = tuple(hashlib.sha256(f.payload).hexdigest() for f in prepared if f.camera != "left_wrist")
                if pair in paired_active_pixels:
                    assert paired_active_pixels[pair] == digest, "Active-camera pixels changed between variants"
                else:
                    paired_active_pixels[pair] = digest
            with (args.output/"requests.jsonl").open("a") as stream:
                stream.write(json.dumps(record) + "\n")
            if index % 40 == 0:
                print(json.dumps(dict(completed=index+1, success=valid,
                                     network_rtt_ms=record["network_rtt_ms"])), flush=True)
            time.sleep(max(0., args.interval-(time.perf_counter()-started)))
    finally:
        transport.close()
        encoder.close()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    selected = [r for r in records if r["index"] >= 10 and r["success"]]
    def aggregate(rows):
        metrics = {key: stats([r[key] for r in rows]) for key in (
            "payload_bytes", "image_bytes", "pixel_preprocess_png_ms", "envelope_ms", "pack_ms",
            "client_total_ms", "network_rtt_ms", "rtt_minus_handler_ms", "rtt_minus_worker_ms")}
        for key in ("decode", "handler", "log_and_replay", "worker_total", "worker_queue"):
            metrics["server_"+key+"_ms"] = stats([r["server"][key] for r in rows])
        for label, begin, end in (
            ("parent_send", "parent_send_started_ns", "parent_send_completed_ns"),
            ("send_to_child", "parent_send_started_ns", "child_received_ns"),
            ("child_reply_to_parent", "child_reply_started_ns", "parent_received_ns"),
            ("parent_decode", "parent_received_ns", "parent_decoded_ns"),
            ("child_decode", "ws_received_ns", "child_decoded_ns")):
            metrics[label+"_ms"] = stats([(r["transport_stages"][end]-r["transport_stages"][begin])/1e6 for r in rows])
        return metrics
    variants = {key: aggregate([r for r in selected if r["variant"] == key]) for key in sorted({r["variant"] for r in records})}
    metrics = variants["png1"] if not args.alternate_masked_left else {}
    summary = dict(source="historical_images_live_F1_to_H20_echo_only", source_run=str(args.source_run),
        started_wall_ns=started_wall_ns, finished_wall_ns=time.time_ns(), session_id=session,
        catalog_release=catalog["release"], model=args.model, endpoint=endpoint+"/echo", health=health,
        count=len(records), requested=args.count, unique_observations=len(samples),
        warmup_excluded=10, steady_success_count=len(selected),
        failed_requests=sum(not r["success"] for r in records), interval_s=args.interval,
        robot_commands=0, model_inference_calls=0, live_camera_subscription=False,
        pixel_workers=workers, metrics=metrics, variant_metrics=variants,
        paired_masked_left=args.alternate_masked_left, active_pixel_pairs_verified=len(paired_active_pixels),
        client_source_sha256={str(path):hashlib.sha256(Path(path).read_bytes()).hexdigest()
            for path in (pixels_module.__file__, websocket_module.__file__)},
        limitations=["Echo uses real request parsing and PNG decode, but no model/IK.",
            "Echo response is smaller than a real model response.",
            "RTT minus handler/worker includes network, SSH, OS scheduling, response serialization and client decoding; it is not pure network latency.",
            "Historical replay has no live camera, recording, or control load.",
            "First 10 requests are reported separately; no failed requests are discarded."])
    (args.output/"summary.json").write_text(json.dumps(summary, indent=2)+"\n")
    with (args.output/"metrics.csv").open("w") as stream:
        writer=csv.DictWriter(stream,fieldnames=["variant","metric","p50","p95","p99","max"])
        writer.writeheader()
        for variant, ms in variants.items():
            for key, value in ms.items():writer.writerow(dict(variant=variant,metric=key,**value))
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()

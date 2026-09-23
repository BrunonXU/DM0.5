#!/usr/bin/env python3
"""Preview the exact selected 000083 replay bundle; never execute robot actions."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import html
import io
import json
from pathlib import Path
import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from mcap.reader import make_reader
from mcap_ros2.decoder import DecoderFactory

from render_three_camera_videos import CAMERAS, image_payload
from audit_camera_timestamps import schema_supported

TASK = Path(__file__).resolve().parents[1]
EPISODE = "all_data_3_derived/episode_000083"
RAW = Path("/mnt/dm05_nvme/dm05/datasets/f1_foam_sort/raw")
NAMES = ("head", "left", "right")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=TASK / "results/demo_778_tail/replay_bundles/all_data_3_derived_episode_000083")
    parser.add_argument("--output", type=Path, default=TASK / "results/20260914/videos/replay_000083")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    bundle_path = args.bundle / "manifest.json"
    bundle = json.loads(bundle_path.read_text())
    if bundle["source"]["episode"] != EPISODE:
        raise ValueError("This preview is bound to episode 000083")
    frame_path = args.bundle / bundle["frames"]["path"]
    if frame_path.stat().st_size != bundle["frames"]["size_bytes"] or sha(frame_path) != bundle["frames"]["sha256"]:
        raise ValueError("Bundle frame hash/size mismatch")
    frames = [json.loads(line) for line in frame_path.read_text().splitlines()]
    if len(frames) != bundle["frames"]["count"] or len(frames) != 771:
        raise ValueError("Unexpected bundle frame count")
    source_npz = Path(bundle["source"]["aligned_npz"]["path"])
    source_json = Path(bundle["source"]["aligned_json"]["path"])
    for key, path in (("aligned_npz", source_npz), ("aligned_json", source_json)):
        if sha(path) != bundle["source"][key]["sha256"]:
            raise ValueError("Bundle source mask pack changed")
    meta = json.loads(source_json.read_text())
    source_start = meta["start_ns"]
    begin, stop = bundle["source"]["start_index"], bundle["source"]["stop_index_exclusive"]
    with np.load(source_npz, allow_pickle=False) as pack:
        times = pack["time_ns"][begin:stop].copy()
        expected_end = int(pack["time_ns"][stop])
        for name in ("state_valid", "action_candidate_valid", "joint_frame_candidate_valid", "future_signal_action_candidate_valid"):
            if not pack[name][begin:stop].all():
                raise ValueError("Selected bundle intersects invalid mask: " + name)
        if stop != meta["tail_mask_start_index"] or pack["action_candidate_valid"][stop:].any():
            raise ValueError("Unexpected conservative masked tail boundary")
        np.testing.assert_array_equal(times, [frame["source_time_ns"] for frame in frames])
        np.testing.assert_array_equal(pack["state"][begin:stop], [frame["state"] for frame in frames])
        np.testing.assert_array_equal(pack["action"][begin:stop], [frame["action"] for frame in frames])
        camera_indices = pack["camera_source_indices"][begin:stop:2].copy()
    if expected_end != bundle["timing"]["end_exclusive_source_time_ns"] or int(times[0]) != bundle["timing"]["relative_origin_ns"]:
        raise ValueError("Bundle timing does not match the selected mask pack")
    if expected_end - int(times[0]) != bundle["duration_ns"] or bundle["duration_ns"] != 25_700_000_000:
        raise ValueError("Unexpected replay duration")
    camera_path = TASK / "results/cleaning_v1/cameras" / (EPISODE + ".npz")
    if sha(camera_path) != meta["camera_npz_sha256"]:
        raise ValueError("Camera index hash mismatch")
    with np.load(camera_path, allow_pickle=False) as camera:
        selected_logs, selected_headers = {}, {}
        for col, name in enumerate(NAMES):
            idx = camera_indices[:, col]
            if np.any(idx < 0) or not camera[name + "_timestamp_candidate_valid"][idx].all():
                raise ValueError("Selected preview camera index is invalid")
            selected_logs[name] = camera[name + "_log_ns"][idx].copy()
            selected_headers[name] = camera[name + "_header_ns"][idx].copy()
            if np.any(selected_logs[name] > times[::2]) or np.any(selected_logs[name] >= expected_end):
                raise ValueError("Preview camera source crosses current time or excluded tail")
    raw = RAW / meta["source_relative_path"]
    before = raw.stat()
    if (before.st_size, before.st_mtime_ns) != (meta["source_size_bytes"], meta["source_mtime_ns"]):
        raise ValueError("Raw source stat changed")
    topics = list(CAMERAS.values())
    name_by_topic = dict(zip(topics, NAMES))
    wanted = {topic: set(map(int, selected_logs[name])) for topic, name in name_by_topic.items()}
    seek_start = min(int(values[0]) for values in selected_logs.values())
    stem = "all_data_3_derived_episode_000083_valid"
    video = args.output / (stem + ".mp4")
    partial = args.output / (stem + ".partial.mp4")
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
    small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 15)
    contact = {}
    sheet_indices = {int(round(value / 2) * 2) for value in np.linspace(0, len(frames) - 1, 8)}
    stats = {name: {"jpeg_decoded": 0, "held_over_100ms": 0, "decoder_crosscheck": False} for name in NAMES}
    provenance = []
    latest, cached = {}, {}
    with raw.open("rb") as stream, (args.output / "ffmpeg.log").open("w") as log:
        reader = make_reader(stream, validate_crcs=True)
        summary = reader.get_summary()
        if not summary or not summary.chunk_indexes:
            raise ValueError("MCAP indexed reader required")
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                   "-s", "1920x460", "-r", "30", "-i", "pipe:0", "-an", "-vf", "select=not(mod(n\\,2))",
                   "-vsync", "vfr", "-r", "30", "-c:v", "libx264", "-threads", "4", "-preset", "veryfast",
                   "-crf", "22", "-pix_fmt", "yuv420p", "-video_track_timescale", "30000", "-movflags", "+faststart", str(partial)]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=log)
        messages = iter(reader.iter_messages(topics=topics, start_time=seek_start,
                                              end_time=expected_end, log_time_order=True))
        pending = next(messages, None)
        try:
            for index, now in enumerate(times):
                if index % 2:
                    process.stdin.write(canvas_bytes)  # Dropped by select; no new observation/action is invented.
                    continue
                while pending is not None and pending[2].log_time <= now:
                    schema, channel, message = pending
                    if message.log_time in wanted[channel.topic]:
                        name = name_by_topic[channel.topic]
                        if not stats[name]["decoder_crosscheck"]:
                            if not schema_supported(schema, channel):
                                raise ValueError("Unsupported image schema")
                            stamp, fmt, payload = image_payload(message.data)
                            decoded = DecoderFactory().decoder_for(channel.message_encoding, schema)(message.data)
                            assert payload == bytes(decoded.data) and fmt == decoded.format
                            assert stamp == decoded.header.stamp.sec * 10**9 + decoded.header.stamp.nanosec
                            stats[name]["decoder_crosscheck"] = True
                        latest[channel.topic] = message
                    pending = next(messages, None)
                sample = index // 2
                source_s = (int(now) - source_start) / 1e9
                canvas = Image.new("RGB", (1920, 460), "#101820")
                draw = ImageDraw.Draw(canvas)
                draw.text((12, 5), f'{EPISODE} | source t={source_s:.3f}s | replay t={(int(now)-int(times[0]))/1e9:.3f}s / 25.700s | 1x / 15fps preview', font=font, fill="white")
                source_record = {"preview_frame": sample, "bundle_frame": index, "aligned_source_index": begin + index,
                                 "source_log_ns": str(int(now)), "source_time_s": source_s, "cameras": {}}
                for col, (label, topic) in enumerate(CAMERAS.items()):
                    name, x = name_by_topic[topic], 640 * col
                    message = latest.get(topic)
                    if message is None or message.log_time != int(selected_logs[name][sample]):
                        raise ValueError("Exact selected camera frame not found: " + name)
                    if topic not in cached or cached[topic][0] != message.log_time:
                        stamp, _, payload = image_payload(message.data)
                        if stamp != int(selected_headers[name][sample]):
                            raise ValueError("Camera header/index mismatch")
                        with Image.open(io.BytesIO(payload)) as original:
                            original.load()
                            picture = original.convert("RGB")
                            picture.thumbnail((638, 360), Image.Resampling.BILINEAR)
                        cached[topic] = (message.log_time, stamp, picture)
                        stats[name]["jpeg_decoded"] += 1
                    timestamp, header, picture = cached[topic]
                    age = (int(now) - timestamp) / 1e6
                    draw.text((x + 8, 36), label, font=font, fill="#79caff")
                    canvas.paste(picture, (x + (640 - picture.width) // 2, 64 + (360 - picture.height) // 2))
                    draw.text((x + 8, 426), f'log age {age:.0f}ms | header-log {(header-timestamp)/1e9:+.3f}s',
                              font=small, fill="#ffbf69" if age > 100 else "white")
                    stats[name]["held_over_100ms"] += int(age > 100)
                    source_record["cameras"][name] = {"camera_source_index": int(camera_indices[sample, col]),
                                                      "log_ns": str(timestamp), "header_ns": str(header), "age_ms": age}
                provenance.append(source_record)
                canvas_bytes = canvas.tobytes()
                process.stdin.write(canvas_bytes)
                if index == 0:
                    canvas.save(args.output / "poster.jpg", quality=92)
                if index in sheet_indices:
                    tile = canvas.resize((960, 230), Image.Resampling.LANCZOS)
                    contact[index] = (tile, source_s)
            process.stdin.close()
            if process.wait() != 0:
                raise RuntimeError("Video rendering failed; see ffmpeg.log")
        except BaseException:
            process.kill()
            process.wait()
            raise
    partial.replace(video)
    probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                       "stream=codec_name,width,height,nb_frames,r_frame_rate,avg_frame_rate,duration:format=duration,size", "-of", "json", str(video)]))
    if int(probe["streams"][0]["nb_frames"]) != len(provenance) or probe["streams"][0]["r_frame_rate"] != "15/1":
        raise ValueError("Preview frame count/rate mismatch")
    if abs(float(probe["format"]["duration"]) - 25.7) > 0.000001:
        raise ValueError("Video extends beyond or truncates the selected bundle duration")
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(video), "-f", "null", "-"], capture_output=True, text=True)
    if decoded.returncode or decoded.stderr.strip():
        raise RuntimeError("Full output MP4 decode failed: " + decoded.stderr)
    after = raw.stat()
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise ValueError("Raw source changed during preview")
    sheet = Image.new("RGB", (1920, 40 + 250 * math_ceil_half(len(contact))), "#101820")
    draw = ImageDraw.Draw(sheet)
    draw.text((12, 10), f'{EPISODE} | selected valid source [0.067, 25.767)s | sparse review, not task acceptance', font=font, fill="white")
    sheet_samples = []
    for place, (index, (tile, source_s)) in enumerate(sorted(contact.items())):
        x, y = (place % 2) * 960, 40 + (place // 2) * 250
        sheet.paste(tile, (x, y))
        draw.text((x + 8, y + 230), f'source {source_s:.3f}s | bundle frame {index}', font=small, fill="white")
        sheet_samples.append({"bundle_frame": index, "source_time_s": source_s})
    sheet.save(args.output / "contact_sheet.jpg", quality=93)
    with (args.output / "frame_sources.jsonl").open("w") as handle:
        for record in provenance:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    result = {"created_at": datetime.now(timezone.utc).isoformat(), "episode": EPISODE,
              "status": "offline_selected_bundle_preview_not_robot_execution_acceptance", "bundle_frames": len(frames),
              "bundle_hz": 30, "duration_s": 25.7, "preview_nominal_fps": 15, "preview_frames": len(provenance),
              "last_preview_frame_duration_s": bundle["timing"]["final_hold_ns"] / 1e9,
              "source_interval_s": [(int(times[0]) - source_start) / 1e9, (expected_end - source_start) / 1e9],
              "source_interval_convention": "half_open", "last_preview_source_time_s": provenance[-1]["source_time_s"],
              "tail_mask_start_s": meta["tail_mask_start_s"], "tail_frames_included": 0,
              "unknown_action_padding": False, "robot_or_ros_started": False,
              "raw_source": {"path": str(raw), "size_bytes": before.st_size, "mtime_ns": before.st_mtime_ns,
                             "stat_unchanged": True, "full_sha256_computed": False},
              "bundle_manifest": {"path": str(bundle_path), "sha256": sha(bundle_path)},
              "bundle_frames_file": {"path": str(frame_path), "sha256": sha(frame_path)},
              "masked_aligned_npz": bundle["source"]["aligned_npz"], "masked_aligned_json": bundle["source"]["aligned_json"],
              "camera_npz": {"path": str(camera_path), "sha256": sha(camera_path)},
              "video": {"file": video.name, "sha256": sha(video), "ffprobe": probe},
              "frame_sources_sha256": sha(args.output / "frame_sources.jsonl"), "camera_statistics": stats,
              "contact_sheet": {"file": "contact_sheet.jpg", "samples": sheet_samples},
              "script_sha256": sha(__file__), "checks": {"bundle_values_equal_mask_pack": True,
               "selected_masks_all_valid": True, "all_camera_indices_exact_and_causal": True,
               "all_preview_grid_times_strictly_before_mask_start": bool(np.all(times[::2] < expected_end)),
               "all_camera_log_times_strictly_before_mask_start": all(np.all(v < expected_end) for v in selected_logs.values()),
               "output_duration_equals_bundle": True, "full_output_mp4_decode_passed": True},
              "limits": ["Recorded log-time alignment does not prove physical exposure synchronization.",
                         "Conservative tail boundary is not a certified reset onset and may truncate return motion.",
                         "Base values in replay bundle remain posthoc weak targets; task completion and robot replay are not approved."]}
    write_json(args.output / "manifest.json", result)
    page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>F1 replay候选000083有效区间</title><style>body{{margin:24px auto;padding:0 20px;max-width:1600px;background:#111820;color:#e8f0f6;font:16px/1.65 system-ui}}video,img{{display:block;width:100%;height:auto;background:#000}}a{{color:#82cdff}}.note{{padding:14px 20px;background:#25313c;border-left:4px solid #ffca79}}code{{color:#bbdef5}}.small{{color:#b9cad6;font-size:14px}}</style></head><body><h1>第一次离线replay候选：000083</h1><p><code>{html.escape(EPISODE)}</code>，训练分组中的一段清洗有效区间；左：头部，中：左腕，右：右腕。</p><p><strong>数值bundle：771帧，30Hz，25.7秒。</strong>本页是同一有效区间的<strong>15fps、原速视频预览</strong>，不是把数值动作改成15Hz。</p><p class="note">源时间范围 <strong>[0.066666667, 25.766666667) 秒</strong>，右端不包含；25.766666667秒开始的保守mask尾段没有进入本视频或所选bundle。这个边界不等于已确认的复位起点，返回动作可能被截短，当前不能认定这是一条完整成功循环。</p><video controls preload="metadata" playsinline poster="poster.jpg" src="{video.name}"></video><p><a href="{video.name}" download>下载25.7秒三路视频</a> · <a href="manifest.json">来源、mask、bundle哈希及检查</a> · <a href="frame_sources.jsonl">逐预览帧的原始索引</a> · <a href="../index.html">返回三路视频</a></p><p class="small">预览每两个30Hz bundle时刻取一帧，共386幅；最后一幅仅显示已知的1/30秒结束区间，因此视频总长严格为25.7秒，没有延长到已mask的尾段，也没有补未知动作。三路图像使用清洗包选定的原相机索引，均不晚于显示log时刻；视频标明原始时间差与帧年龄。这是离线内容审阅，未运行ROS或机器人。</p><h2>八个时刻快速查看</h2><p class="small">以下仅为抽帧索引；判断任务内容请播放上方完整有效区间。<a href="contact_sheet.jpg" target="_blank">打开大图</a></p><img src="contact_sheet.jpg" alt="清洗有效区间的八个三路相机时刻"><p class="small">底盘动作仍是后补弱标签；原始数据、清洗包和replay bundle均未改动。此页不表示完成真机回放或训练验收。</p></body></html>'''
    (args.output / "index.html").write_text(page)
    apply_timing_review_disposition(args.output)
    print(json.dumps({"output": str(args.output), "bundle_frames": 771, "preview_frames": len(provenance),
                      "duration_s": 25.7, "full_decode_passed": True, "tail_frames_included": 0}, ensure_ascii=False), flush=True)


def math_ceil_half(n):
    return (n + 1) // 2


def apply_timing_review_disposition(output):
    """Preserve a later timing-review restriction when rebuilding this preview."""
    evidence_path = output / "timing_review/findings.json"
    if not evidence_path.exists():
        return
    evidence = json.loads(evidence_path.read_text())
    if evidence.get("disposition") != "do_not_use_for_full_joint_robot_replay_pending_weak_base_timing_review":
        return
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["status"] = "offline_diagnostic_preview_with_strong_weak_base_timing_mismatch_candidate"
    manifest["disposition"] = evidence["disposition"]
    manifest["disposition_reason"] = "Head-camera static scenery keeps shifting after weak base labels reach zero; recorded head/body joints remain almost constant."
    manifest["timing_review"] = {"path": "timing_review/findings.json", "sha256": sha(evidence_path),
                                "strong_mismatch_candidate": True, "exact_chassis_stop_time_known": False}
    manifest["script_sha256"] = sha(__file__)
    write_json(manifest_path, manifest)
    page_path = output / "index.html"
    page = page_path.read_text()
    if 'id="timing-review-alert"' not in page:
        alert = '<div id="timing-review-alert" role="alert" style="margin:24px 0;padding:20px;background:#542b26;border:2px solid #ffb197;border-radius:8px"><strong>新增时间错配线索；仅作诊断预览，不用于完整联合真机回放</strong><p>21.010秒以后后补底盘目标已为零，但21–23秒头相机中的料架、地面固定物仍明显移位；所录头部和身体反馈几乎不变。数值mask通过不等于弱速度标签通过。这是强错配线索，尚未恢复真实速度或精确停止时间。</p><p><a href="timing_review/head_20_25s.jpg">20–25秒头部对照图</a> · <a href="timing_review/three_camera_20_25s.jpg">三路对照图</a> · <a href="timing_review/findings.md">证据说明</a> · <a href="timing_review/findings.json">证据记录</a></p></div>'
        page = page.replace("<body>", "<body>" + alert, 1)
        page = page.replace("第一次离线replay候选：000083", "000083：时间错配诊断预览")
        page = page.replace("F1 replay候选000083有效区间", "F1 000083 时间错配诊断")
        page_path.write_text(page)


if __name__ == "__main__":
    main()

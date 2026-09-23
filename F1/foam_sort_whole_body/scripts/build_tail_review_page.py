#!/usr/bin/env python3
"""Render two original tail clips with selectable, log-aligned lead/HAL plots.

Read-only MCAP/signals; output is a review aid, never a training mask or a
certified reset boundary. Video rendering uses only source camera JPEGs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import html
import io
import json
import math
from pathlib import Path
import subprocess
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from mcap.reader import make_reader
from mcap_ros2.decoder import DecoderFactory

from render_three_camera_videos import CAMERAS, image_payload
from audit_camera_timestamps import schema_supported

TASK = Path(__file__).resolve().parents[1]
EXAMPLES = [("all_data_derived/episode_000192", 4), ("all_data_derived/episode_000093", 7)]
CAMERA_NAMES = {topic: name for topic, name in zip(CAMERAS.values(), ("head", "left", "right"))}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def causal_signals(signal_path, grid):
    result = {}
    with np.load(signal_path, allow_pickle=False) as arrays:
        for stream, key in (("lead", "lead_q"), ("hal", "hal_q"), ("base", "base_v")):
            times, values = arrays[stream + "_t_ns"], arrays[key][:, :14]
            indices = np.searchsorted(times, grid, side="right") - 1
            if np.any(indices < 0):
                raise ValueError("Missing earlier arm sample in tail review")
            chosen = values[indices]
            if not np.all(np.isfinite(chosen)):
                raise ValueError("Nonfinite numeric source")
            result[stream] = chosen.tolist()
            result[stream + "_source_indices"] = indices.tolist()
            result[stream + "_source_log_ns"] = [str(int(t)) for t in times[indices]]
            result[stream + "_age_ms"] = ((grid - times[indices]) / 1e6).tolist()
    return result


def add_base_source_note(result):
    row = result["provenance"]["candidate_row"]
    if sha(row["signal_npz_path"]) != row["signal_npz_sha256"]:
        raise ValueError("Signal source changed before adding base readout")
    with np.load(row["signal_npz_path"], allow_pickle=False) as values:
        nonzero = np.flatnonzero(np.any(values["base_v"] != 0, axis=1))
        result["base_last_nonzero_source_sample_s"] = ((int(values["base_t_ns"][nonzero[-1]]) - row["source_start_ns"]) / 1e9
                                                        if len(nonzero) else None)
    result["provenance"]["base_source"] = "posthoc weak base_v label sampled causally; not measured chassis state"
    result["provenance"]["base_last_nonzero_definition"] = "any raw vx/vy/wz component != 0; reports actual MCAP sample, not continuous label interval endpoint"


def render(row, tail, joint, output, seconds, fps):
    started = time.monotonic()
    raw = Path(row["raw_path"])
    before = raw.stat()
    if (before.st_size, before.st_mtime_ns) != (row["source_fingerprint"]["size_bytes"], row["source_fingerprint"]["mtime_ns"]):
        raise ValueError("Source stat differs from candidate identity")
    for key in ("signal", "camera"):
        if sha(row[key + "_npz_path"]) != row[key + "_npz_sha256"]:
            raise ValueError(key + " NPZ hash differs from source catalog")
    origin = row["source_start_ns"]
    end = row["source_stop_ns_exclusive"] - 1
    clip_start = max(origin, end - round(seconds * 1e9))
    frame_count = math.ceil((end - clip_start) / 1e9 * fps)
    grid = np.array([clip_start + round(index * 1e9 / fps) for index in range(frame_count)], dtype=np.int64)
    numeric = causal_signals(row["signal_npz_path"], grid)
    with np.load(row["camera_npz_path"], allow_pickle=False) as arrays:
        camera_logs = {camera: arrays[camera + "_log_ns"].copy() for camera in CAMERA_NAMES.values()}
    initial = []
    for logs in camera_logs.values():
        prior = np.searchsorted(logs, clip_start, side="right") - 1
        if prior >= 0:
            initial.append(int(logs[prior]))
    seek_start = min(initial + [clip_start])
    stem = row["episode"].replace("/", "_") + "_tail"
    target, temporary = output / (stem + ".mp4"), output / (stem + ".partial.mp4")
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
    small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 15)
    size = (1920, 460)
    camera_rows = {name: [] for name in CAMERA_NAMES.values()}
    stats = {name: {"jpeg_decoded": 0, "missing_video_frames": 0, "held_over_100ms_video_frames": 0,
                    "decoder_crosscheck": False} for name in CAMERA_NAMES.values()}
    latest, cached = {}, {}
    with raw.open("rb") as stream, (output / (stem + ".ffmpeg.log")).open("w") as log:
        reader = make_reader(stream, validate_crcs=True)
        summary = reader.get_summary()
        if not summary or not summary.chunk_indexes:
            raise ValueError("MCAP indexed seeking required")
        if (summary.statistics.message_start_time, summary.statistics.message_end_time) != (origin, end):
            raise ValueError("MCAP summary bounds differ from source catalog")
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
                   "-pix_fmt", "rgb24", "-s", "1920x460", "-r", str(fps), "-i", "pipe:0", "-an",
                   "-c:v", "libx264", "-threads", "4", "-preset", "veryfast", "-crf", "22",
                   "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary)]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=log)
        messages = iter(reader.iter_messages(topics=list(CAMERAS.values()), start_time=seek_start,
                                              end_time=end + 1, log_time_order=True))
        pending = next(messages, None)
        try:
            for index, now in enumerate(grid):
                while pending is not None and pending[2].log_time <= now:
                    schema, channel, message = pending
                    name = CAMERA_NAMES[channel.topic]
                    if not stats[name]["decoder_crosscheck"]:
                        if not schema_supported(schema, channel):
                            raise ValueError("Unexpected image schema")
                        stamp, fmt, payload = image_payload(message.data)
                        decoded = DecoderFactory().decoder_for(channel.message_encoding, schema)(message.data)
                        assert stamp == decoded.header.stamp.sec * 10**9 + decoded.header.stamp.nanosec
                        assert fmt == decoded.format and payload == bytes(decoded.data)
                        stats[name]["decoder_crosscheck"] = True
                    latest[channel.topic] = message
                    pending = next(messages, None)
                frame = Image.new("RGB", size, "#101820")
                draw = ImageDraw.Draw(frame)
                relative = (int(now) - origin) / 1e9
                draw.text((12, 5), f'{row["episode"]}    source t={relative:.3f}s / {(end-origin)/1e9:.3f}s    1x / {fps}fps', font=font, fill="white")
                for column, (label, topic) in enumerate(CAMERAS.items()):
                    name, x = CAMERA_NAMES[topic], column * 640
                    draw.text((x + 8, 36), label, font=font, fill="#78ccff")
                    message = latest.get(topic)
                    if message is None:
                        stats[name]["missing_video_frames"] += 1
                        camera_rows[name].append(None)
                        draw.text((x + 8, 190), "NO PRIOR CAMERA FRAME", font=font, fill="#ffbf69")
                        continue
                    if topic not in cached or cached[topic][0] != message.log_time:
                        stamp, fmt, payload = image_payload(message.data)
                        with Image.open(io.BytesIO(payload)) as picture:
                            picture.load()
                            rgb = picture.convert("RGB")
                            rgb.thumbnail((638, 360), Image.Resampling.BILINEAR)
                        cached[topic] = (message.log_time, stamp, rgb)
                        stats[name]["jpeg_decoded"] += 1
                    timestamp, header, picture = cached[topic]
                    source_index = int(np.searchsorted(camera_logs[name], int(now), side="right") - 1)
                    if source_index < 0 or int(camera_logs[name][source_index]) != timestamp:
                        raise ValueError("Video camera sample differs from original camera log index")
                    age_ms = (int(now) - timestamp) / 1e6
                    camera_rows[name].append({"source_index": source_index, "log_ns": str(timestamp),
                                              "header_ns": str(header), "age_ms": age_ms})
                    frame.paste(picture, (x + (640 - picture.width) // 2, 64 + (360 - picture.height) // 2))
                    draw.text((x + 8, 426), f'log age {age_ms:.0f}ms | header-log {(header-timestamp)/1e9:+.3f}s',
                              font=small, fill="#ffbf69" if age_ms > 100 else "white")
                    stats[name]["held_over_100ms_video_frames"] += int(age_ms > 100)
                process.stdin.write(frame.tobytes())
                if index == 0:
                    frame.save(output / (stem + ".jpg"), quality=92)
            process.stdin.close()
            if process.wait() != 0:
                raise RuntimeError("ffmpeg render failed: " + stem)
        except BaseException:
            process.kill()
            process.wait()
            raise
    temporary.replace(target)
    probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                       "stream=codec_name,width,height,nb_frames,r_frame_rate:format=duration,size", "-of", "json", str(target)]))
    if int(probe["streams"][0]["nb_frames"]) != frame_count or abs(float(probe["format"]["duration"]) - frame_count / fps) > 0.02:
        raise ValueError("Output video timing mismatch")
    check = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(target), "-f", "null", "-"],
                           capture_output=True, text=True)
    if check.returncode or check.stderr.strip():
        raise RuntimeError("Output full video decode failed: " + check.stderr)
    after = raw.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Raw source stat changed during rendering")
    result = {"episode": row["episode"], "file": target.name, "poster": stem + ".jpg", "data_file": stem + ".json",
              "poster_video_frame": 0, "poster_source_time_s": (clip_start - origin) / 1e9,
              "raw_path": str(raw), "source_fingerprint": row["source_fingerprint"],
              "source_stat_unchanged": True, "source_start_ns": str(origin), "source_end_ns": str(end),
              "clip_start_ns": str(clip_start), "clip_end_ns": str(end),
              "clip_start_s": (clip_start - origin) / 1e9, "clip_end_s": (end - origin) / 1e9,
              "time_s": ((grid - origin) / 1e9).tolist(), "grid_log_ns": [str(int(v)) for v in grid],
              "fps": fps, "output_frames": frame_count, "playback_speed": 1, "default_joint": joint,
              "joint_names": [side + "_j" + str(j) for side in ("left", "right") for j in range(1, 8)],
              "unit": "degree_as_stored_in_recorded_arm_signals", "numeric": numeric,
              "held_start_s": tail["held_start_s"], "held_last_s": tail["held_last_s"],
              "held_duration_s": tail["held_duration_s"], "divergence_hint_s": tail["first_gt_2deg_extra_error_s"],
              "held_boundary_status": tail["boundary_status"],
              "camera_frame_sources": camera_rows, "camera_statistics": stats,
              "provenance": {"candidate_row": row, "signal_npz_sha256": row["signal_npz_sha256"],
                             "camera_npz_sha256": row["camera_npz_sha256"],
                             "numeric_source": "signals raw lead_q/hal_q first 14 columns, no unit/sign conversion",
                             "sample_rule": "last raw camera/signal sample at or before each shared 15Hz MCAP log grid point",
                             "training_timestamp_masks_applied": False,
                             "exposure_synchronization_claim": False, "physical_reset_onset_claim": False,
                             "raw_mcap_full_sha256_computed": False},
              "ffprobe": probe, "full_output_ffmpeg_decode_passed": True,
              "video_sha256": sha(target), "elapsed_s": time.monotonic() - started}
    write_json(output / result["data_file"], result)
    print(f'{row["episode"]}: {frame_count} frames, {float(probe["format"]["duration"]):.2f}s, full ffmpeg decode passed', flush=True)
    return result


PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>F1 尾部动作核对 · 三路画面与目标/反馈</title>
<style>
:root{color-scheme:dark}body{margin:0;background:#101820;color:#e9f0f6;font:16px/1.6 system-ui,sans-serif}main{max-width:1500px;margin:auto;padding:24px}h1{font-size:28px}h2{font-size:22px;margin:0}p{margin:10px 0}.intro,.note{background:#192731;padding:16px 20px;border-radius:10px}.note{border-left:4px solid #ffbf69}article{margin:28px 0 44px;padding:20px;background:#15212b;border:1px solid #30414e;border-radius:12px}video{width:100%;display:block;background:#000;margin:14px 0}.controls{display:flex;gap:16px;align-items:center;flex-wrap:wrap}select,button{font:inherit;background:#243641;color:white;padding:6px 10px;border:1px solid #557183;border-radius:6px;cursor:pointer}a{color:#83d0ff}.lead{color:#63baff}.hal{color:#ffae63}.held{color:#ffdc86}.numbers{font-variant-numeric:tabular-nums;font-size:15px}.chart{width:100%;background:#0e1820;border-radius:8px;margin-top:10px;touch-action:manipulation;cursor:crosshair}.small{font-size:14px;color:#bbcad6}.numbers span{display:inline-block;margin-right:20px}details{font-size:14px;color:#b9cbd8}summary{cursor:pointer}.error{color:#ff8b8b}footer{color:#abc0ce;font-size:14px}
</style></head><body><main>
<h1>尾部到底在做什么：画面与双臂记录一起看</h1>
<div class="intro"><p>以下两条均为原始录制<strong>最后12秒</strong>，三路画面按原速播放：左为头部，中为左腕，右为右腕。视频左上角和曲线横轴均保留<strong>整条MCAP起点后的秒数</strong>。</p>
<p><span class="lead">蓝线 /lead：普通示教目标</span>；<span class="hal">橙线 /hal：实际关节反馈</span>。选择任一关节，拖动视频或点击曲线定位，观察目标保持不变时反馈和手臂画面是否仍在变化。下方同步显示底盘vx/vy/wz后补目标读数，它们是<strong>弱标签，不是底盘实测</strong>。</p></div>
<p class="note"><strong>“3.8秒”是788条候选尾段长度的中位数，不是每条都为3.8秒，也不是自动裁掉末尾3.8秒。</strong>黄色区域从“末尾目标保持不变”的保守候选起点开始；这个起点<strong>并非已经确认的真实复位开始时间</strong>，其中可以包含正常保持。两条例证只能支持“可能存在另一条复位控制路径”，不能确认采集版本或具体触发原因。</p>
__CARDS__
<footer><p>按同一15Hz MCAP log网格取不晚于当前时刻的最近图像、lead及HAL样本。保留原始时钟差和旧帧年龄，不进行相机曝光校正、不平滑数值、不修改训练mask。画面与曲线在记录时间上对应，不等于相机曝光与控制物理同步已经验收。</p><p>记录与校验：<a href="manifest.json">manifest.json</a> · <a href="verification.json">verification.json</a> · <a href="../index.html">返回原三路视频</a></p></footer>
</main><script>
const DATA = __DATA__;
const NS='http://www.w3.org/2000/svg';
function node(type,attrs={},text=''){const n=document.createElementNS(NS,type);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);if(text)n.textContent=text;return n;}
for(const [i,d]of DATA.entries()){
 const card=document.querySelector(`#example-${i}`), video=card.querySelector('video'), sel=card.querySelector('select'), svg=card.querySelector('svg');
 d.joint_names.forEach((name,j)=>{const o=document.createElement('option');o.value=j;o.textContent=(j<7?'左臂':'右臂')+' J'+(j%7+1);sel.appendChild(o)});sel.value=d.default_joint;
 const W=1120,H=300,p={l:65,r:24,t:33,b:42}, x=t=>p.l+(t-d.clip_start_s)/(d.clip_end_s-d.clip_start_s)*(W-p.l-p.r);
 let cursor, leadDot, halDot, y;
 function draw(){
  svg.replaceChildren();const j=Number(sel.value),a=d.numeric.lead.map(v=>v[j]),b=d.numeric.hal.map(v=>v[j]);let lo=Math.min(...a,...b),hi=Math.max(...a,...b),pad=Math.max((hi-lo)*.12,1);lo-=pad;hi+=pad;y=v=>H-p.b-(v-lo)/(hi-lo)*(H-p.t-p.b);
  const held=Math.max(d.clip_start_s,Math.min(d.clip_end_s,d.held_start_s));svg.appendChild(node('rect',{x:x(held),y:p.t,width:x(d.clip_end_s)-x(held),height:H-p.t-p.b,fill:'#806021',opacity:.22}));
  for(let k=0;k<=4;k++){let v=lo+(hi-lo)*k/4,yy=y(v);svg.appendChild(node('line',{x1:p.l,x2:W-p.r,y1:yy,y2:yy,stroke:'#2b3b47'}));svg.appendChild(node('text',{x:p.l-8,y:yy+5,'text-anchor':'end',fill:'#bdccd7','font-size':13},v.toFixed(1)));}
  for(let k=0;k<=6;k++){let t=d.clip_start_s+(d.clip_end_s-d.clip_start_s)*k/6;svg.appendChild(node('text',{x:x(t),y:H-16,'text-anchor':'middle',fill:'#bdccd7','font-size':13},t.toFixed(2)+'s'));}
  svg.appendChild(node('text',{x:12,y:20,fill:'#bdccd7','font-size':13},'度 (°)'));
  svg.appendChild(node('line',{x1:x(held),x2:x(held),y1:p.t,y2:H-p.b,stroke:'#ffdc86','stroke-dasharray':'5 4'}));svg.appendChild(node('text',{x:Math.min(x(held)+5,W-225),y:21,fill:'#ffdc86','font-size':13},'目标保持候选 '+d.held_start_s.toFixed(3)+'s'));
  function path(values,color){let data='M '+x(d.time_s[0])+' '+y(values[0]);for(let n=1;n<values.length;n++)data+=' H '+x(d.time_s[n])+' V '+y(values[n]);svg.appendChild(node('path',{d:data,fill:'none',stroke:color,'stroke-width':2.5}));}path(a,'#63baff');path(b,'#ffae63');
  cursor=node('line',{y1:p.t,y2:H-p.b,stroke:'white','stroke-width':1.3});leadDot=node('circle',{r:4,fill:'#63baff'});halDot=node('circle',{r:4,fill:'#ffae63'});svg.append(cursor,leadDot,halDot);update();
 }
 function update(){const j=Number(sel.value),frame=Math.max(0,Math.min(d.output_frames-1,Math.floor((video.currentTime||0)*d.fps+1e-6))),t=d.time_s[frame],a=d.numeric.lead[frame][j],b=d.numeric.hal[frame][j];cursor.setAttribute('x1',x(t));cursor.setAttribute('x2',x(t));leadDot.setAttribute('cx',x(t));leadDot.setAttribute('cy',y(a));halDot.setAttribute('cx',x(t));halDot.setAttribute('cy',y(b));card.querySelector('.clock').textContent=t.toFixed(3)+'s（视频 '+(video.currentTime||0).toFixed(2)+'s）';card.querySelector('.lead-value').textContent=a.toFixed(3)+'°';card.querySelector('.hal-value').textContent=b.toFixed(3)+'°';card.querySelector('.age').textContent='lead '+d.numeric.lead_age_ms[frame].toFixed(1)+'ms / HAL '+d.numeric.hal_age_ms[frame].toFixed(1)+'ms / base '+d.numeric.base_age_ms[frame].toFixed(1)+'ms';for(const[k,n]of ['vx','vy','wz'].entries())card.querySelector('.base-'+n).textContent=d.numeric.base[frame][k].toFixed(4);}
 svg.addEventListener('click',e=>{const r=svg.getBoundingClientRect(),px=(e.clientX-r.left)/r.width*W;video.currentTime=Math.max(0,Math.min(d.clip_end_s-d.clip_start_s-.001,(px-p.l)/(W-p.l-p.r)*(d.clip_end_s-d.clip_start_s)));update();});
 card.querySelector('.jump-held').addEventListener('click',()=>{video.currentTime=Math.max(0,d.held_start_s-d.clip_start_s-.8);video.play().catch(()=>{});});
 sel.addEventListener('change',draw);for(const evt of ['timeupdate','seeked','loadedmetadata'])video.addEventListener(evt,update);video.addEventListener('play',()=>{const tick=()=>{update();if(!video.paused&&!video.ended)requestAnimationFrame(tick)};requestAnimationFrame(tick)});draw();
}
</script></body></html>'''


def build_page(results, output):
    cards = []
    for index, result in enumerate(results):
        joint = result["default_joint"]
        text = "左臂J5：目标保持约−30°，反馈向0°变化" if joint == 4 else "右臂J1：目标保持约−80°，反馈继续向−110°变化"
        cards.append(f'''<article id="example-{index}"><h2>{html.escape(result['episode'])}</h2>
<p>{text}。结合头部和腕部画面核对手臂是否正在回到休息姿态。</p>
<p class="small">本片 {result['clip_start_s']:.3f}–{result['clip_end_s']:.3f}s；目标保持候选 {result['held_start_s']:.3f}–{result['held_last_s']:.3f}s，长度 <strong>{result['held_duration_s']:.3f}s</strong>。</p>
<video controls preload="metadata" playsinline poster="{result['poster']}" src="{result['file']}"></video>
<div class="controls"><label>查看关节 <select aria-label="选择左右臂关节"></select></label><button class="jump-held">从候选起点前0.8秒播放</button><a href="{result['file']}" download>下载12秒三路视频</a></div>
<svg class="chart" viewBox="0 0 1120 300" role="img" aria-label="所选关节目标与反馈曲线，点击定位视频"></svg>
<div class="numbers"><span>整条时间 <b class="clock"></b></span><span class="lead">lead <b class="lead-value"></b></span><span class="hal">HAL <b class="hal-value"></b></span></div>
<div class="numbers held"><span>底盘后补目标（非实测）</span><span>vx <b class="base-vx"></b> m/s</span><span>vy <b class="base-vy"></b> m/s</span><span>wz <b class="base-wz"></b> rad/s</span></div>
<p class="small">普通双臂目标保持时，后补底盘目标仍可能非零。本条原始后补速度最后非零采样点为 {result['base_last_nonzero_source_sample_s']:.3f}s；不能把普通双臂held尾段机械等同于整条任务已经结束，也不能依据后补值确认底盘实际运动。</p>
<p class="small">当前原始样本距显示时刻：<span class="age"></span>。曲线为因果保持采样，不插值。黄色区域不代表已判定整段无效。</p>
<details><summary>时间与文件来源</summary><p>视频与曲线共用{result['fps']}Hz log网格。JPEG来自原MCAP三路相机，lead/HAL来自同源raw信号提取包，原始角度直接显示；所有帧的原始索引、时间与包SHA见<a href="{result['data_file']}">本条记录</a>。没有将真实复位时刻设为held起点，也没有修改数据或训练监督。</p></details></article>''')
    # Keep the browser payload small; complete provenance stays in per-clip JSON.
    keys = ("episode", "clip_start_s", "clip_end_s", "time_s", "fps", "output_frames", "default_joint",
            "joint_names", "numeric", "held_start_s")
    data = [{k: result[k] for k in keys} for result in results]
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    (output / "index.html").write_text(PAGE.replace("__CARDS__", "\n".join(cards)).replace("__DATA__", encoded))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=TASK / "results/20260914/videos/tail_review")
    parser.add_argument("--seconds", type=float, default=12)
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--reuse-videos", action="store_true", help="Refresh plot data/page using already verified, unchanged MP4s")
    args = parser.parse_args()
    if args.seconds <= 0 or args.fps <= 0:
        parser.error("seconds and fps must be positive")
    args.output.mkdir(parents=True, exist_ok=True)
    audit = TASK / "results/demo_788_check"
    candidate_path, tail_path = audit / "candidates.jsonl", audit / "signals/tail_reset_candidates.jsonl"
    candidates = {r["episode"]: r for r in map(json.loads, candidate_path.read_text().splitlines())}
    tails = {r["episode"]: r for r in map(json.loads, tail_path.read_text().splitlines())}
    results = []
    for episode, joint in EXAMPLES:
        if args.reuse_videos:
            result = json.loads((args.output / (episode.replace("/", "_") + "_tail.json")).read_text())
            if sha(args.output / result["file"]) != result["video_sha256"] or not result["full_output_ffmpeg_decode_passed"]:
                raise ValueError("Previously verified video changed")
            result["numeric"] = causal_signals(candidates[episode]["signal_npz_path"], np.array(result["grid_log_ns"], dtype=np.int64))
        else:
            result = render(candidates[episode], tails[episode], joint, args.output, args.seconds, args.fps)
        add_base_source_note(result)
        write_json(args.output / result["data_file"], result)
        results.append(result)
    build_page(results, args.output)
    lengths = [tail["held_duration_s"] for tail in tails.values()]
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "script_sha256": sha(__file__),
                "candidate_manifest_sha256": sha(candidate_path), "tail_candidate_manifest_sha256": sha(tail_path),
                "held_candidate_count": len(lengths), "held_candidate_median_s": float(np.median(lengths)),
                "held_candidate_min_s": min(lengths), "held_candidate_max_s": max(lengths),
                "held_boundary_is_exact_reset_onset": False, "training_masks_modified": False,
                "poster_selection": "clip frame 0 at video time 0, matching initial numeric plot cursor",
                "outputs": [{k: result[k] for k in ("episode", "file", "data_file", "poster", "clip_start_s", "clip_end_s",
                                                        "output_frames", "fps", "video_sha256", "full_output_ffmpeg_decode_passed")}
                            for result in results], "index_sha256": sha(args.output / "index.html")}
    write_json(args.output / "manifest.json", manifest)
    write_json(args.output / "verification.json", {"success": True, "videos": len(results),
               "decoded_output_frames": sum(r["output_frames"] for r in results),
               "camera_frame_index_checks": sum(sum(x is not None for x in rows) for r in results for rows in r["camera_frame_sources"].values()),
               "all_output_videos_full_ffmpeg_decode_passed": all(r["full_output_ffmpeg_decode_passed"] for r in results),
               "raw_source_stat_unchanged": all(r["source_stat_unchanged"] for r in results),
               "raw_files_modified": 0, "training_masks_modified": False,
               "check_scope": "source identity, causal camera/signal samples, exact camera index match, frame count/duration, whole output MP4 decode"})
    print("Tail review page complete: " + str(args.output / "index.html"), flush=True)


if __name__ == "__main__":
    main()

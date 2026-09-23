#!/usr/bin/env python3
"""Offline three-camera review video. Original journal/payload remain untouched."""
import argparse
from bisect import bisect_right
import json
import hashlib
import io
import math
from pathlib import Path
import subprocess
from PIL import Image, ImageDraw, ImageFont, ImageOps


def render(episode, output):
    obs = [json.loads(l) for l in (episode/'payload/frames.jsonl').open()]
    controls = [json.loads(l) for l in (episode/'journal/control.jsonl').open()]
    events = [json.loads(l) for l in (episode/'journal/events.jsonl').open()]
    times = [r['available_monotonic_ns'] for r in obs]
    control_times = [r['monotonic_ns'] for r in controls]
    transitions = {'policy_to_hil':'hil', 'hold_to_hil':'hil',
                   'hil_to_handoff_hold':'hold', 'fresh_policy_to_policy':'policy',
                   'safe_hold_entered':'safe'}
    edges = sorted([e for e in events if e['event'] in transitions], key=lambda e:e['monotonic_ns'])
    edge_times = [e['monotonic_ns'] for e in edges]
    start, end = times[0], times[-1]
    duration = (end-start)/1e9
    labels = {
        'wait': ('等待连接 / 首次控制', (142,153,174)),
        'policy': ('DM0.5 策略执行', (67,169,255)),
        'policy_wait': ('DM0.5 等待推理 / 保持目标', (92,133,180)),
        'hil': ('HIL 人工操作', (255,163,67)),
        'human_hold': ('HIL 人工持有控制权 / 暂停操作', (218,172,92)),
        'home': ('HIL · Y 自动回 Home（非人工纠正标签）', (183,138,246)),
        'hold': ('HOLD · 等待新观测与新策略', (168,185,204)),
        'safe': ('SAFE HOLD · 保护保持', (245,96,96))}
    if any(e['event'] == 'runtime_waiting_for_start' for e in events):
        labels['wait'] = ('等待首次 START / 尚未开始控制', (142,153,174))
    def state_at(now):
        c = bisect_right(control_times, now)-1
        e = bisect_right(edge_times, now)-1
        state = transitions[edges[e]['event']] if e >= 0 else 'policy'
        if state in ('hold','safe'): return state
        if state == 'hil':
            source = controls[c]['control_source'] if c >= 0 else ''
            return source if source in ('home','human_hold') else 'hil'
        if c < 0: return 'wait'
        return 'policy_wait' if controls[c]['control_source'] == 'policy_wait' else 'policy'
    font_path='/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
    fonts={n:ImageFont.truetype(font_path,n) for n in (18,22,30)}
    width,height,fps=1440,570,30
    frames=math.ceil(duration*fps)+1
    states=[state_at(start+round(i*1e9/fps)) for i in range(frames)]
    segments=[]
    for i, state in enumerate(states):
        if not segments or segments[-1]['state'] != state:
            if segments: segments[-1]['end_seconds']=i/fps
            segments.append(dict(start_seconds=i/fps,state=state,label=labels[state][0]))
    segments[-1]['end_seconds']=frames/fps
    output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists(): raise FileExistsError(output)
    ffmpeg=subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','error','-y',
        '-f','rawvideo','-pixel_format','rgb24','-video_size',f'{width}x{height}',
        '-framerate',str(fps),'-i','-','-an','-c:v','libx264','-preset','fast',
        '-crf','22','-pix_fmt','yuv420p','-movflags','+faststart',str(output)],stdin=subprocess.PIPE)
    last_index=-1; panels=[]
    camera_names=('头部 HEAD','左腕 LEFT WRIST','右腕 RIGHT WRIST')
    try:
        for i,state in enumerate(states):
            now=start+round(i*1e9/fps)
            idx=max(0,bisect_right(times,now)-1)
            if idx != last_index:
                panels=[]
                for frame in obs[idx]['frames']:
                    payload = (episode/'payload'/frame['path']).read_bytes()
                    if len(payload) != frame['payload_bytes'] or hashlib.sha256(payload).hexdigest() != frame['payload_sha256']:
                        raise ValueError('payload size/hash differs from recording index')
                    with Image.open(io.BytesIO(payload)) as image:
                        if image.size != (frame['width'],frame['height']): raise ValueError('frame dimensions differ')
                        panels.append(ImageOps.contain(image.convert('RGB'),(472,330)))
                last_index=idx
            canvas=Image.new('RGB',(width,height),(17,22,32));draw=ImageDraw.Draw(canvas)
            label,color=labels[state]
            draw.rectangle((0,0,width,64),fill=color)
            draw.text((20,10),label,font=fonts[30],fill=(12,18,25))
            draw.text((1100,16),f'{i/fps:06.2f}s / {duration:06.2f}s',font=fonts[22],fill=(12,18,25))
            for j,panel in enumerate(panels):
                x=j*480
                draw.text((x+12,75),camera_names[j],font=fonts[22],fill='white')
                canvas.paste(panel,(x+(480-panel.width)//2,110+(330-panel.height)//2))
                frame=obs[idx]['frames'][j]
                draw.text((x+12,445),f"frame_id={frame['frame_id']}  读取后 {(now-frame['host_received_monotonic_ns'])/1e6:.0f}ms",font=fonts[18],fill=(199,207,218))
            draw.text((16,478),'原速回放 · 按 host monotonic 对齐 · 30fps展示沿用最近已到达图像，不生成新帧',font=fonts[18],fill=(190,201,220))
            for seg in segments:
                x0=int(seg['start_seconds']/(frames/fps)*width);x1=int(seg['end_seconds']/(frames/fps)*width)
                draw.rectangle((x0,514,x1,532),fill=labels[seg['state']][1])
            x=min(width-1,int(i/max(1,frames-1)*width));draw.line((x,510,x,537),fill='white',width=3)
            draw.text((16,541),episode.name+'  |  REVIEW ONLY · training_ready=false',font=fonts[18],fill=(151,163,184))
            ffmpeg.stdin.write(canvas.tobytes())
            if i==int(fps*30): canvas.save(output.with_suffix('.jpg'))
            if i % (fps*30)==0: print(f'rendered {i/fps:.0f}/{duration:.0f}s',flush=True)
        ffmpeg.stdin.close()
        if ffmpeg.wait()!=0: raise RuntimeError('ffmpeg failed')
    except BaseException:
        ffmpeg.kill();ffmpeg.wait();raise
    output.with_suffix('.json').write_text(json.dumps(dict(episode=episode.name,
        source_time='host available_monotonic_ns; latest available image, no future frames',
        render_fps=fps,playback_speed=1,training_ready=False,segments=segments),ensure_ascii=False,indent=2))
    print(output,flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('episode',type=Path);p.add_argument('output',type=Path)
    a=p.parse_args();render(a.episode,a.output)

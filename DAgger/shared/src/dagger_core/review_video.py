"""Robot-neutral offline three-view compositor, adapted from G2's 42 renderer.
Uses the same latest-observation timeline, contain/letterbox, provenance labels
and H.264 pipeline. No control imports; G2 legacy entry stays unchanged.
"""
from bisect import bisect_right
import io
import math
from pathlib import Path
import subprocess
from PIL import Image, ImageDraw, ImageFont, ImageOps

COLORS={'hil_latched':(55,126,220),'policy':(139,90,205),'handoff_hold':(208,153,36),'safe_hold':(205,61,64)}

def render(window,observations,controls,read_image,output,caption):
    times=[r['available_monotonic_ns'] for r in observations];ct=[c['monotonic_ns'] for c in controls]
    start,end=window['start_ns'],window['end_ns'];fps=30;width,height=1440,570
    count=max(1,math.ceil((end-start)/1e9*fps)+1)
    fontpath=Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
    if not fontpath.exists():fontpath=Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    font=ImageFont.truetype(str(fontpath),18)
    proc=subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','error','-n','-f','rawvideo','-pixel_format','rgb24','-video_size',f'{width}x{height}','-framerate',str(fps),'-i','-','-an','-c:v','libx264','-preset','fast','-crf','22','-pix_fmt','yuv420p','-movflags','+faststart',str(output)],stdin=subprocess.PIPE)
    timeline=[];last=-1;panels=[]
    try:
        for i in range(count):
            now=min(end,start+round(i*1e9/fps));index=bisect_right(times,now)-1
            if index<0:raise ValueError('no causal image for clip start')
            row=observations[index];ci=bisect_right(ct,now)-1;c=controls[ci] if ci>=0 else {}
            state=c.get('hil_state','waiting');source=c.get('source','unknown');color=COLORS.get(state,(110,120,130))
            if index!=last:
                panels=[]
                for f in row['frames']:
                    with Image.open(io.BytesIO(read_image(row,f))) as image:
                        if image.size!=(f['width'],f['height']):raise ValueError('source image dimensions mismatch')
                        panels.append(ImageOps.contain(image.convert('RGB'),(472,330)))
                last=index
            canvas=Image.new('RGB',(width,height),(17,22,32));draw=ImageDraw.Draw(canvas)
            draw.rectangle((0,0,width,64),fill=color);draw.text((16,16),f'{state} / {source} | intervention {window["intervention_id"]} | {i/fps:.2f}s',font=font,fill='white')
            for j,panel in enumerate(panels):
                frame=row['frames'][j];x=j*480;draw.text((x+12,75),frame['camera'],font=font,fill='white');canvas.paste(panel,(x+(480-panel.width)//2,110+(330-panel.height)//2))
                draw.text((x+12,445),f'frame {frame["frame_id"]} | age {(now-frame["host_received_monotonic_ns"])/1e6:.0f}ms',font=font,fill='white')
            draw.text((16,478),caption[:120],font=font,fill='white')
            span=max(1,end-start)
            for marker in ('takeover_ns','handoff_ns','resume_ns'):
                stamp=window.get(marker)
                if stamp is not None:
                    x=max(0,min(width-1,int((stamp-start)/span*width)));draw.line((x,542,x,565),fill='white',width=2)
            draw.rectangle((0,548,int((now-start)/span*width),555),fill=color)
            draw.text((16,512),'30fps display only / held images are NOT new acquisitions / training_ready=false',font=font,fill='white')
            # Preserve exact raw indices and held-frame provenance for each display tick.
            timeline.append(dict(display_index=i,time_ns=now,observation_id=row['observation_id'],control_index=ci,state=state,source=source,held=index==bisect_right(times,max(start,now-round(1e9/fps)))-1 and i>0))
            proc.stdin.write(canvas.tobytes())
        proc.stdin.close()
        if proc.wait()!=0:raise RuntimeError('ffmpeg failed')
    except BaseException:
        proc.kill();proc.wait();raise
    # Actually decode output before returning a completed artifact.
    subprocess.run(['ffmpeg','-v','error','-i',str(output),'-f','null','-'],check=True,stdout=subprocess.DEVNULL)
    return timeline

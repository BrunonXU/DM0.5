"""Build a self-contained listening page; no robot runtime changes."""
import array
import base64
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
import wave

TASK = Path(__file__).resolve().parents[1]
PROJECTS = TASK.parents[1]
OUT = TASK/'DataPrehandle/visualizations/20261021-hil-audio'
RATE = 16000


def wav(samples):
    peak=max(abs(x) for x in samples)
    samples=[x*.55/max(peak,1e-9) for x in samples]
    raw=array.array('h',(round(x*32767) for x in samples))
    stream=io.BytesIO()
    with wave.open(stream,'wb') as w:
        w.setparams((1,2,RATE,0,'NONE','not compressed'));w.writeframes(raw.tobytes())
    return stream.getvalue()


def bell(seconds,notes,brass=False):
    result=[]
    for i in range(round(seconds*RATE)):
        t=i/RATE;v=0.
        for start,freq,gain in notes:
            u=t-start
            if u<0:continue
            attack=1-math.exp(-u/(.035 if brass else .009))
            decay=math.exp(-u/(.21 if brass else .26))
            harmonics=((1,1),(2,.25),(3,.12),(4,.05)) if brass else ((1,1),(2.76,.18),(5.4,.045))
            v+=gain*attack*decay*sum(a*math.sin(2*math.pi*freq*h*u) for h,a in harmonics)
        fade=min(1,max(0,(seconds-t)/.10))
        result.append(v*fade)
    return wav(result)


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    clips={
      'a':dict(title='低沉双钟',tag='备选 · 低沉',desc='两下低钟，第二下稍轻。短促、有分量，接管边界清楚。',origin='Kenney bong_001 · 降调、双击、短回响 · CC0',data=(TASK/'artifacts/20261021-rtc-hil/audio-candidates/takeover-solemn-double.wav').read_bytes()),
      'b':dict(title='沉稳单钟',tag='更克制',desc='一声低钟，柔和落下。仪式感较轻，适合频繁介入。',origin='原创程序合成 · 196 Hz基音与钟声泛音',data=bell(.9,[(0,196,1)])),
      'c':dict(title='下行双音',tag='已选定 · 已部署',desc='先高后低，两音落定。强调“现在由人接管”。',origin='原创程序合成 · 294→220 Hz · F1现场确认提高约4.6dB',data=(PROJECTS/'DAgger/F1/src/f1dagger/audio/takeover.wav').read_bytes()),
      'd':dict(title='低铜管和弦',tag='更庄重',desc='低音和弦缓起、收束，厚实而稳定；四个里最有仪式感。',origin='原创程序合成 · 147/220/294 Hz和弦',data=bell(1.05,[(0,146.83,1),(0,220,.55),(0,293.66,.28)],True)),
      'start':dict(title='第一次 START · 策略开始',tag='现有音',desc='保留原开始音作对照。',origin='Kenney confirmation_001 · CC0',data=(PROJECTS/'DAgger/F1/src/f1dagger/audio/started.wav').read_bytes()),
      'resume':dict(title='第三次 START · 交还生效',tag='交还提示 · 已部署',desc='上行双音；在新策略实际生效时提示。与人工介入区别开。',origin='原创程序合成 · 440→660 Hz',data=bell(.62,[(0,440,.8),(.18,660,.85)])),
      'saved':dict(title='结束保存',tag='现有音 · 独立于START',desc='完整保存后播放原B音。',origin='Kenney confirmation_002 · CC0',data=(PROJECTS/'DAgger/F1/src/f1dagger/audio/saved.wav').read_bytes())}
    manifest={}
    for key,c in clips.items():
        data=c.pop('data');(OUT/(key+'.wav')).write_bytes(data)
        with wave.open(io.BytesIO(data),'rb') as w:c['seconds']=round(w.getnframes()/w.getframerate(),2)
        manifest[key]=dict(c,sha256=hashlib.sha256(data).hexdigest())
        c['url']='data:audio/wav;base64,'+base64.b64encode(data).decode()
    template=(TASK/'scripts/hil_audio_review.template.html').read_text()
    (OUT/'index.html').write_text(template.replace('__CLIPS__',json.dumps(clips,ensure_ascii=False)))
    (OUT/'manifest.json').write_text(json.dumps(dict(selected_takeover='c',deployed_clips=['start','c','resume','saved'],release='rtc-hil-r4',clips=manifest),ensure_ascii=False,indent=2)+'\n')
    shutil.copy2(PROJECTS/'F1-4242/results/audio-review-20261013/source/interface/License.txt',OUT/'Kenney-CC0.txt')
    print(OUT/'index.html')


if __name__=='__main__':main()

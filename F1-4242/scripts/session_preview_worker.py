#!/usr/bin/env python3
"""Server-only full-session review worker. No ROS or robot output imports.
Uses the workbench's authenticated, resumable, rate-limited media queue.
"""
import argparse
import base64
from bisect import bisect_right
import hashlib
import http.cookiejar
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPCookieProcessor, ProxyHandler, HTTPRedirectHandler

VERSION='f1-session-review-v1'
ENCODING='native-dimensions-v2'
CAMERAS={'head':'head','left_wrist':'left','right_wrist':'right'}
FILES=['journal/manifest.json','journal/observations.jsonl','journal/control.jsonl','journal/events.jsonl','payload/manifest.json','payload/frames.jsonl']

def cache_job_root(cache,job):
    key=job.get('id','');source=job.get('source_sha256','')
    if not re.fullmatch('[a-f0-9]{64}',key) or not re.fullmatch('[a-f0-9]{64}',source):raise ValueError('invalid cache identity')
    if key!=hashlib.sha256((source+VERSION).encode()).hexdigest():raise ValueError('cache identity mismatch')
    cache=Path(cache).resolve();root=cache/key
    if root.is_symlink():raise ValueError('symlink cache job forbidden')
    return root

def receipt_path(cache,job):
    root=cache_job_root(cache,job)
    directory=root.parent.with_name(root.parent.name+'-receipts')
    if directory.is_symlink():raise ValueError('symlink receipt directory forbidden')
    directory.mkdir(exist_ok=True)
    path=directory/(job['id']+'.json')
    if path.is_symlink():raise ValueError('symlink receipt forbidden')
    return path

def save_receipt(path,value):
    tmp=path.with_suffix('.tmp')
    with tmp.open('w') as f:
        json.dump(value,f);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)

def cleanup_published_job(cache,job):
    """Delete only one identity-bound local job after a durable publication receipt."""
    root=cache_job_root(cache,job);path=receipt_path(cache,job)
    receipt=json.loads(path.read_text())
    if receipt.get('published') is not True or receipt.get('id')!=job['id'] or receipt.get('source_sha256')!=job['source_sha256'] or receipt.get('renderer')!=VERSION:raise ValueError('publication not confirmed')
    if root.exists():shutil.rmtree(root)
    receipt.update(cache_removed=True,cleaned_unix=time.time());save_receipt(path,receipt)

def finish_cache(cache,job,files,timing=None):
    receipt=dict(id=job['id'],source_sha256=job['source_sha256'],episode_id=job['episode_id'],renderer=VERSION,
                 published=True,published_unix=time.time(),files=files,cache_removed=False,timing=timing or {})
    save_receipt(receipt_path(cache,job),receipt)
    cleanup_published_job(cache,job)

def retry_cleanup(cache):
    directory=Path(cache).resolve().with_name(Path(cache).resolve().name+'-receipts')
    if directory.is_symlink():raise ValueError('symlink receipt directory forbidden')
    for path in directory.glob('*.json'):
        try:
            if path.is_symlink():continue
            receipt=json.loads(path.read_text())
            if receipt.get('published') is True and not receipt.get('cache_removed'):
                cleanup_published_job(cache,receipt)
        except (OSError,ValueError,KeyError) as exc:print('cache cleanup pending:',path.name,str(exc),flush=True)

def retry_publications(client,cache):
    # A lost HTTP response may leave a completed robot job unclaimable. Reconcile
    # only jobs whose uploads completed and whose source-bound publication is ready.
    for root in Path(cache).glob('*'):
        pending=root/'publication-pending.json'
        if root.is_symlink() or not pending.is_file() or pending.is_symlink():continue
        try:
            value=json.loads(pending.read_text());job=value['job']
            if cache_job_root(cache,job)!=root.resolve():raise ValueError('pending cache identity mismatch')
            if client.published(job):finish_cache(cache,job,value['files'],value.get('timing'))
        except (ValueError,KeyError) as exc:print('publication check pending:',root.name,str(exc),flush=True)


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('redirect forbidden')

class Client:
    def __init__(self,config):
        self.url=config['robot_url'].rstrip('/');p=urlsplit(self.url)
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.path or p.query or p.fragment:raise ValueError('invalid robot URL')
        self.origin=config.get('request_origin',self.url).rstrip('/')
        origin=urlsplit(self.origin)
        if origin.scheme not in ('http','https') or not origin.hostname or origin.username or origin.password or origin.path or origin.query or origin.fragment:raise ValueError('invalid request origin')
        self.host=origin.netloc
        self.pause_s=0.;self.download_bytes=0;self.upload_bytes=0
        self.opener=build_opener(ProxyHandler({}),NoRedirect(),HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.post('/api/login',{})
    def published(self,job):
        eid=job.get('episode_id','')
        if not re.fullmatch('[a-f0-9]{24}',eid):raise ValueError('invalid episode identity')
        req=Request(self.url+'/api/data/episodes/'+eid,headers={'Host':self.host})
        with self.opener.open(req,timeout=20) as r:value=json.load(r)
        row=value.get('episode',{})
        return (value.get('ok') is True and row.get('preview_status')=='ready'
                and row.get('metadata_sha256')==job['source_sha256']
                and row.get('source_signature')==job['source_signature']
                and {v.get('camera') for v in row.get('videos',[])}=={'head','left','right'})
    def post(self,path,body):
        req=Request(self.url+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Origin':self.origin,'Host':self.host,'X-F1-Request':'1'})
        try:
            with self.opener.open(req,timeout=20) as r:value=json.load(r)
        except HTTPError as exc:
            if exc.code==401 or exc.code>=500:raise URLError('workbench temporarily unavailable or session expired') from exc
            detail=json.loads(exc.read(65536));raise ValueError(detail.get('error',str(exc))) from exc
        if not value.get('ok'):raise ValueError(value.get('error','request failed'))
        return value
    def op(self,op,job,**kw):return self.post('/api/hil/media/'+op,dict(id=job['id'],lease=job['lease'],**kw))
    def transfer(self,op,job,**kw):
        while True:
            try:return self.op(op,job,**kw)
            except ValueError as exc:
                if 'transfer_paused_active_session' not in str(exc):raise
                started=time.monotonic()
                self.op('progress',job,status='returning' if op in ('upload','publish_preview') else 'transferring');time.sleep(2)
                self.pause_s+=time.monotonic()-started
    def block_size(self,job,direction):
        value=job.get('transfer',{}).get(direction+'_block_bytes',131072)
        if type(value) is not int or not 1<=value<=1024*1024:raise ValueError('invalid negotiated block size')
        return value
    def download(self,job,name,path):
        path.parent.mkdir(parents=True,exist_ok=True)
        offset=path.stat().st_size if path.exists() else 0
        with path.open('ab') as f:
            while True:
                r=self.transfer('source',job,name=name,offset=offset,count=self.block_size(job,'source'))
                data=base64.b64decode(r['data'],validate=True)
                if r.get('offset')!=offset or len(data)>self.block_size(job,'source') or offset+len(data)>r['size']:raise ValueError('invalid source range response')
                f.write(data);offset+=len(data);self.download_bytes+=len(data)
                if offset==r['size']:break
                if not data:raise ValueError('source truncated')
    def upload(self,job,path):
        offset=0;size=path.stat().st_size
        with path.open('rb') as f:
            while offset<size:
                f.seek(offset);data=f.read(self.block_size(job,'upload'))
                offset=self.transfer('upload',job,name=path.name,offset=offset,data=base64.b64encode(data).decode())['offset']
                self.upload_bytes+=len(data)
                if not 0<offset<=size:raise ValueError('invalid resume offset')


def render(root,output,source_sha):
    from PIL import Image, ImageDraw
    from io import BytesIO
    rows=lambda name:[json.loads(line) for line in (root/name).read_text().splitlines() if line]
    frames=rows('payload/frames.jsonl');obs=rows('journal/observations.jsonl');control=rows('journal/control.jsonl')
    if not frames or len(frames)>120000:raise ValueError('empty or oversized session')
    times=[int(r['capture_started_monotonic_ns']) for r in frames];start=times[0];end=times[-1]
    if any(b<=a for a,b in zip(times,times[1:])):raise ValueError('nonmonotonic images')
    fps=30;count=math.ceil((end-start)/1e9*fps)+1;duration=count/fps
    output.mkdir(parents=True,exist_ok=True)
    mapping=[];states=[];actions=[];atimes=[];last_ids={};handles={};encoders={};logs={};cache={};dimensions={}
    ot=[int(r['available_monotonic_ns']) for r in obs];ct=[int(r['monotonic_ns']) for r in control]
    if any(b<a for a,b in zip(ot,ot[1:])) or any(b<a for a,b in zip(ct,ct[1:])):raise ValueError('nonmonotonic source events')
    def values(row,key):
        v=row.get(key)
        return [float(x) if isinstance(x,(float,int)) and math.isfinite(x) else None for x in v] if isinstance(v,list) and len(v)==16 else [None]*16
    def original_frame(camera,frame):
        if frame.get('path')!=camera+'.pack' or frame.get('storage')!='f1-camera-pack-v1':raise ValueError('invalid image pack')
        offset,size=frame['payload_offset'],frame['payload_bytes']
        if type(offset) is not int or offset<0 or type(size) is not int or not 0<size<=8*1024*1024:raise ValueError('invalid payload range')
        handle=handles[camera];handle.seek(offset);data=handle.read(size)
        if len(data)!=size or hashlib.sha256(data).hexdigest()!=frame['payload_sha256']:raise ValueError('frame integrity failure')
        with Image.open(BytesIO(data)) as original:return original.convert('RGB')
    try:
        for camera,name in CAMERAS.items():
            handles[camera]=(root/'payload'/f'{camera}.pack').open('rb')
            first=original_frame(camera,next(f for f in frames[0]['frames'] if f['camera']==camera))
            width,height=dimensions[camera]=first.size
            if not 0<width<=8192 or not 0<height<=8192 or width%2 or height%2:raise ValueError('unsupported native video dimensions; use original frames')
            logs[camera]=(output/f'{name}.log').open('wb')
            encoders[camera]=subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','rawvideo','-pix_fmt','rgb24','-s',f'{width}x{height}','-r','30','-i','pipe:0','-an','-c:v','libx264','-preset','veryfast','-crf','18','-threads','1','-pix_fmt','yuv420p','-movflags','+faststart',str(output/f'{name}.mp4')],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=logs[camera])
        for n in range(count):
            ns=start+round(n*1e9/fps);i=max(0,bisect_right(times,ns)-1);row=frames[i];record=dict(time_s=n/fps,observation_id=row['observation_id'],source_index=i,cameras={})
            for camera in CAMERAS:
                f=next(x for x in row['frames'] if x['camera']==camera)
                if f.get('path')!=camera+'.pack' or f.get('storage')!='f1-camera-pack-v1':raise ValueError('invalid image pack')
                age=(ns-int(f['host_received_monotonic_ns']))/1e9
                # A group can finish after the review tick. Never display a future camera frame.
                prior=i
                while age<0 and prior>0:
                    prior-=1;f=next(x for x in frames[prior]['frames'] if x['camera']==camera);age=(ns-int(f['host_received_monotonic_ns']))/1e9
                fresh=0<=age<=.25
                identity=f['payload_sha256'];source_identity=(f['frame_id'],f['host_received_monotonic_ns']);repeat=last_ids.get(camera)==source_identity;last_ids[camera]=source_identity
                record['cameras'][camera]=dict(source_index=prior,frame_id=f['frame_id'],received_ns=f['host_received_monotonic_ns'],age_s=age,repeated=repeat,blank=not fresh)
                image=Image.new('RGB',dimensions[camera])
                if fresh:
                    if cache.get(camera,(None,))[0]!=identity:
                        image=original_frame(camera,f)
                        if image.size!=dimensions[camera]:raise ValueError('native image dimensions changed within recording')
                        cache[camera]=(identity,image)
                    image=cache[camera][1]
                else:ImageDraw.Draw(image).text((160,240),'NO FRESH RECORDED FRAME',fill=(220,220,220))
                encoders[camera].stdin.write(image.tobytes())
            mapping.append(record)
            oi=bisect_right(ot,ns)-1;ci=bisect_right(ct,ns)-1
            states.append(values(obs[oi],'state16') if oi>=0 and ns-ot[oi]<=250_000_000 else [None]*16)
            c=control[ci] if ci>=0 else {}
            # selected/candidate targets and measured feedback are never action labels.
            published=c.get('submitted') if c.get('publication_complete') is True and c.get('execution_mode')=='powered' else None
            actions.append(values({'action':published},'action') if ci>=0 and ns-ct[ci]<=250_000_000 else [None]*16)
            atimes.append(n/fps)
        for p in encoders.values():p.stdin.close()
        for p in encoders.values():
            if p.wait(timeout=90):raise ValueError('ffmpeg failed')
    finally:
        for p in encoders.values():
            if p.poll() is None:p.kill();p.wait()
        for h in [*handles.values(),*logs.values()]:h.close()
    for camera,name in CAMERAS.items():
        info=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-count_frames','-show_entries','stream=nb_read_frames,avg_frame_rate,codec_name,width,height','-of','json',str(output/f'{name}.mp4')]))['streams'][0]
        if int(info['nb_read_frames'])!=count or info['avg_frame_rate']!='30/1' or info['codec_name']!='h264':raise ValueError('encoded video validation failed')
        if (info['width'],info['height'])!=dimensions[camera]:raise ValueError('native dimensions not preserved')
    timeline=dict(renderer=VERSION,encoding=ENCODING,native_dimensions=dimensions,video_is_original=False,source_sha256=source_sha,training_ready=False,fps=30,duration_s=duration,source_frames=len(frames),frames=mapping)
    (output/'timeline.json').write_text(json.dumps(timeline,separators=(',',':')))
    analytics=dict(source_sha256=source_sha,timestamps_s=atimes,state=states,action=actions,channel_names=[f'{side}臂J{i}' for side in ('左','右') for i in range(1,8)]+['左夹爪','右夹爪'],units=['deg']*14+['%']*2,duration_s=duration,source_frame_count=len(frames),notes=['30fps展示时间轴，采用本机接收时间；帧保持、断流见timeline索引。','仅使用明确的实际发布目标作为action；未知值留空，不使用候选或反馈回填。','完整会话包含等待、Home和异常段；审阅视频不直接作为训练集。'])
    (output/'analytics.json').write_text(json.dumps(analytics,ensure_ascii=False,separators=(',',':')))
    return {name:dict(bytes=(output/name).stat().st_size,sha256=sha(output/name)) for name in ('head.mp4','left.mp4','right.mp4','timeline.json','analytics.json')}


def process(client,job,cache):
    if job.get('kind')!='session_preview' or job.get('renderer')!=VERSION:raise ValueError('wrong job contract')
    root=cache_job_root(cache,job);phase=['transferring'];stop=threading.Event();errors=[]
    started=time.monotonic();phase_started=started
    initial=(client.pause_s,client.download_bytes,client.upload_bytes)
    timing=dict(queue_age_s=max(0,time.time()-job.get('created_unix',time.time())))
    def stage(name):
        nonlocal phase_started
        now=time.monotonic();timing[phase[0]+'_s']=round(now-phase_started,3)
        phase[0]=name;phase_started=now
        print(json.dumps(dict(episode=job['episode_id'],phase=name,timing=timing)),flush=True)
    print(json.dumps(dict(episode=job['episode_id'],phase=phase[0],timing=timing)),flush=True)
    def heartbeat():
        while not stop.wait(20):
            try:client.op('progress',job,status=phase[0])
            except Exception as exc:errors.append(exc);return
    thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    try:
        for name in FILES:client.download(job,name,root/name)
        m=json.loads((root/'journal/manifest.json').read_text())
        if not m.get('complete') or sha(root/'journal/manifest.json')!=job['source_sha256']:raise ValueError('manifest mismatch')
        for stream in ('observations','control','events'):
            if sha(root/'journal'/f'{stream}.jsonl')!=m['stream_sha256'][stream]:raise ValueError('journal mismatch')
        for name in ('manifest.json','frames.jsonl'):
            if sha(root/'payload'/name)!=m['payload_sha256'][name]:raise ValueError('payload index mismatch')
        pm=json.loads((root/'payload/manifest.json').read_text())
        if not pm.get('complete'):raise ValueError('payload not complete')
        for camera in CAMERAS:
            name=camera+'.pack';client.download(job,'payload/'+name,root/'payload'/name)
            if sha(root/'payload'/name)!=pm['pack_files'][camera]['sha256']:raise ValueError('image pack mismatch')
        stage('rendering');client.op('progress',job,status=phase[0]);out=root/'render';meta=render(root,out,job['source_sha256'])
        if errors:raise errors[0]
        stage('returning');client.op('progress',job,status=phase[0])
        for name in meta:client.upload(job,out/name)
        binding={k:job[k] for k in ('id','source_sha256','source_signature','episode_id')}
        save_receipt(root/'publication-pending.json',dict(job=binding,files=meta,timing=timing))
        published=client.transfer('publish_preview',job,files=meta)
        if published.get('ready') is not True:raise ValueError('preview publication not confirmed; cache retained')
        stage('ready')
        timing.update(total_s=round(time.monotonic()-started,3),paused_s=round(client.pause_s-initial[0],3),download_bytes=client.download_bytes-initial[1],upload_bytes=client.upload_bytes-initial[2])
        print(json.dumps(dict(ready=job['episode_id'],files=meta,timing=timing)),flush=True)
    finally:stop.set();thread.join(2)
    # Cleanup failure must not change the already-published robot job to failed.
    try:finish_cache(cache,job,meta,timing)
    except (OSError,ValueError) as exc:print('published; cache cleanup pending:',job['id'],str(exc),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',required=True);p.add_argument('--once',action='store_true');a=p.parse_args()
    config=json.loads(Path(a.config).read_text());cache=Path(config['cache']).resolve();cache.mkdir(parents=True,exist_ok=True)
    client=None
    while True:
        job=None
        try:
            retry_cleanup(cache)
            if client is None:client=Client(config)
            retry_publications(client,cache)
            job=client.post('/api/hil/media/claim',{'kind':'session_preview'}).get('job')
            if job:process(client,job,cache)
        except Exception as exc:
            print('session preview:',str(exc),flush=True)
            if client and job and not isinstance(exc,(URLError,OSError,TimeoutError)):
                try:client.op('progress',job,status='failed',error=str(exc))
                except Exception:pass
            client=None
        if a.once:break
        time.sleep(10)
if __name__=='__main__':main()

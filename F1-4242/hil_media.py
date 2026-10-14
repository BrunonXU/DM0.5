"""Persistent derived video jobs. Bounded transfer; never touches control output."""
import base64
import hashlib
import json
from pathlib import Path
import re
import threading
import time
import uuid

VERSION='f1-three-view-v1'
PREVIEW_VERSION='f1-session-review-v1'
PREVIEW_FILES={'head.mp4','left.mp4','right.mp4','timeline.json','analytics.json'}
SOURCE_FILES={'journal/manifest.json','journal/observations.jsonl','journal/control.jsonl','journal/events.jsonl','payload/manifest.json','payload/frames.jsonl','payload/head.pack','payload/left_wrist.pack','payload/right_wrist.pack'}

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def atomic(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix('.tmp')
    with tmp.open('w') as f:json.dump(value,f,ensure_ascii=False,allow_nan=False);f.flush();__import__('os').fsync(f.fileno())
    tmp.replace(path)

class HILMedia:
    def __init__(self,hil,busy):
        self.hil,self.busy=hil,busy;self.lock=threading.RLock();self.scan_at=0;self.last_transfer=0;self.known=set()
    def root(self):
        from hil_service import small_json
        return Path(small_json(self.hil.config_path)['runtime']).expanduser().resolve()
    def queue(self):return self.root()/'video-jobs'
    def path(self,key):
        if not re.fullmatch('[a-f0-9]{64}',key):raise ValueError('invalid job id')
        return self.queue()/(key+'.json')
    def load(self,key):return json.loads(self.path(key).read_text())
    def save(self,job):atomic(self.path(job['id']),job)
    def scan(self):
        if time.monotonic()-self.scan_at<10 or self.busy():return
        self.scan_at=time.monotonic()
        try:root=self.root()
        except (OSError,KeyError,ValueError):return
        from hil_data import roots
        for path in (p for r in roots(self.hil) for p in r.glob('*/journal/manifest.json')):
            if str(path) in self.known:continue
            raw=path.read_bytes();m=json.loads(raw);b=m.get('binding',{})
            if m.get('complete'):self.known.add(str(path))
            if not m.get('complete') or b.get('purpose') not in ('demonstration','policy_hil') or m.get('writer_error') or m.get('drop_count'):continue
            payload=path.parents[1]/'payload/manifest.json'
            if not payload.exists() or not json.loads(payload.read_text()).get('complete'):continue
            sha=hashlib.sha256(raw).hexdigest()
            preview_key=hashlib.sha256((sha+PREVIEW_VERSION).encode()).hexdigest()
            if not self.path(preview_key).exists():
                from hil_data import HILData
                row=HILData(self.hil).row(path.parents[1])
                if row and row['recording_status']=='committed':self.save(dict(schema='f1_video_job_v1',id=preview_key,kind='session_preview',episode_id=row['id'],source_signature=row['source_signature'],run_id=row['run_id'],source_sha256=sha,renderer=PREVIEW_VERSION,status='waiting_server',clips=[],created_unix=time.time(),training_ready=False))
            if not b.get('auto_video'):continue
            key=hashlib.sha256((sha+VERSION).encode()).hexdigest()
            if not self.path(key).exists():self.save(dict(schema='f1_video_job_v1',id=key,run_id=path.parents[1].name,source_sha256=sha,renderer=VERSION,status='waiting_server',clips=[],created_unix=time.time(),training_ready=False))
    def listing(self):
        # Read derived small records only. Discovery runs in a background thread.
        try:
            with self.lock:
                paths=sorted(self.queue().glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:100]
                jobs=[json.loads(p.read_text()) for p in paths]
                jobs=[j for j in jobs if j.get('kind','intervention')=='intervention']
                configured=(self.root()/'video-server.json').exists()
                for j in jobs:
                    j.pop('lease',None)
                    if j['status']=='waiting_server' and not configured:j['status']='waiting_config'
                return sorted(jobs,key=lambda j:j.get('created_unix',0),reverse=True)[:100]
        except (FileNotFoundError,KeyError):return []
    def claim(self,kind="intervention"):
        if kind not in ("intervention","session_preview"):raise ValueError("unknown worker kind")
        with self.lock:
            self.scan()
            if self.busy():return dict(paused=True,job=None)
            now=time.time()
            jobs=[json.loads(p.read_text()) for p in self.queue().glob('*.json')]
            if any(j.get('lease_until',0)>now for j in jobs):return dict(paused=False,job=None)
            for j in sorted(jobs,key=lambda j:j.get('created_unix',0)):
                if j.get('kind','intervention')==kind and j['status'] not in ('ready','failed'):
                    j.update(status='transferring',lease=uuid.uuid4().hex,lease_until=now+120)
                    self.save(j);return dict(paused=False,job=j)
            return dict(paused=False,job=None)
    def leased(self,key,token):
        j=self.load(key)
        if not token or j.get('lease')!=token or j.get('lease_until',0)<time.time():raise ValueError('expired video worker lease')
        j['lease_until']=time.time()+120;self.save(j);return j
    def source(self,key,token,name,offset,count):
        with self.lock:
            if self.busy():raise ValueError('transfer_paused_active_session')
            j=self.leased(key,token)
            if name not in SOURCE_FILES or type(offset) is not int or offset<0 or type(count) is not int or not 1<=count<=131072:raise ValueError('invalid source range')
            from hil_data import find_run, confined
            run=find_run(self.hil,j['run_id'])
            p=confined(run,name).resolve()
            if run not in p.parents:raise ValueError('source escaped')
            if digest(run/'journal/manifest.json')!=j['source_sha256']:raise ValueError('source manifest changed')
            # Global single-stream bandwidth ceiling, pause checked each block.
            wait=self.last_transfer+count/(2*1024*1024)-time.monotonic()
            if wait>0:time.sleep(wait)
            self.last_transfer=time.monotonic()
            with p.open('rb') as f:f.seek(offset);data=f.read(count)
            return dict(data=base64.b64encode(data).decode(),size=p.stat().st_size,offset=offset)
    def command(self,operation,body):
        key=body.get('id','')
        with self.lock:
            if operation=='claim':
                atomic(self.root()/'video-server.json',dict(schema='f1_video_worker_registration_v1',last_seen_unix=time.time()))
                return self.claim(body.get('kind','intervention'))
            if operation in ('retry','review'):
                j=self.load(key)
                if operation=='retry':
                    if j['status']!='failed':raise ValueError('only failed jobs can retry')
                    j.update(status='waiting_server',error=None,lease_until=0)
                else:
                    if body.get('review') not in ('pending','accepted','excluded'):raise ValueError('invalid review')
                    clips=j.get('clips',[]);clip=next((c for c in clips if c['name']==body.get('clip')),None)
                    if clip is None:raise ValueError('unknown clip')
                    clip.update(review=body['review'],reviewed_unix=time.time(),review_note=str(body.get('note',''))[:2000],training_ready=False)
                self.save(j);return dict(job=j)
            j=self.leased(key,body.get('lease'))
            if operation=='progress':
                if body.get('status') not in ('transferring','rendering','returning','failed'):raise ValueError('invalid progress')
                j.update(status=body['status'],error=str(body.get('error',''))[:500]);self.save(j);return dict(paused=self.busy())
            if self.busy():raise ValueError('transfer_paused_active_session')
            if operation=='publish_preview':return self.publish_preview(j,body)
            folder=self.root()/'videos'/key;folder.mkdir(parents=True,exist_ok=True)
            name=body.get('name','')
            if j.get('kind')=='session_preview':
                if name not in PREVIEW_FILES:raise ValueError('invalid preview name')
            elif not re.fullmatch(r'clip-[0-9]+\.(mp4|json)',name):raise ValueError('invalid media name')
            path=folder/(name+'.partial')
            if operation=='upload':
                data=base64.b64decode(body.get('data',''),validate=True)
                if len(data)>131072:raise ValueError('upload block too large')
                size=path.stat().st_size if path.exists() else 0
                if body.get('offset')!=size:return dict(offset=size)
                if size+len(data)>512*1024*1024:raise ValueError('clip exceeds 512MiB')
                wait=self.last_transfer+len(data)/(2*1024*1024)-time.monotonic()
                if wait>0:time.sleep(wait)
                self.last_transfer=time.monotonic()
                with path.open('ab') as f:f.write(data)
                return dict(offset=size+len(data))
            if operation=='publish':
                # Each MP4 and its index publish together; source remains immutable.
                if not name.endswith('.mp4'):raise ValueError('mp4 required')
                index=folder/(name[:-4]+'.json.partial')
                if path.stat().st_size!=body.get('bytes') or digest(path)!=body.get('sha256') or digest(index)!=body.get('index_sha256'):raise ValueError('return hash/size mismatch')
                if index.stat().st_size>64*1024*1024:raise ValueError('timeline index exceeds 64MiB')
                meta=json.loads(index.read_text())
                if meta.get('source_sha256')!=j['source_sha256'] or meta.get('renderer')!=VERSION or meta.get('training_ready') is not False:raise ValueError('index binding mismatch')
                with path.open('rb') as f:header=f.read(16)
                if header[4:8]!=b'ftyp':raise ValueError('not MP4')
                index.replace(folder/name.replace('.mp4','.json'));path.replace(folder/name)
                clip=dict(name=name,intervention_id=meta['intervention_id'],review='pending',training_ready=False,url=f'/api/hil/video/{key}/{name}',sha256=body['sha256'],index_sha256=body['index_sha256'],truncated_after=meta.get('truncated_after'))
                old=next((c for c in j['clips'] if c['name']==name),None)
                if old:clip.update({k:old[k] for k in ('review','review_note','reviewed_unix') if k in old})
                j['clips']=[c for c in j['clips'] if c['name']!=name]+[clip];self.save(j);return dict(clip=clip)
            raise ValueError('unknown video operation')
    def publish_preview(self,j,body):
        from hil_data import HILData
        import os
        if j.get('kind')!='session_preview' or j.get('renderer')!=PREVIEW_VERSION:raise ValueError('wrong preview job')
        run,row=HILData(self.hil).locate(j['episode_id'])
        if row['source_signature']!=j['source_signature']:raise ValueError('source changed')
        supplied=body.get('files',{})
        if set(supplied)!=PREVIEW_FILES:raise ValueError('three videos and indices required')
        source=self.root()/'videos'/j['id']
        for name in PREVIEW_FILES:
            p=source/(name+'.partial');meta=supplied[name]
            if p.is_symlink() or p.stat().st_size!=meta.get('bytes') or digest(p)!=meta.get('sha256'):raise ValueError('preview hash/size mismatch')
            if name.endswith('.mp4'):
                with p.open('rb') as f:header=f.read(16)
                if header[4:8]!=b'ftyp':raise ValueError('not MP4')
            elif p.stat().st_size>64*1024*1024:raise ValueError('index too large')
        timeline=json.loads((source/'timeline.json.partial').read_text())
        if timeline.get('source_sha256')!=j['source_sha256'] or timeline.get('renderer')!=PREVIEW_VERSION or timeline.get('training_ready') is not False or timeline.get('fps')!=30:raise ValueError('preview binding mismatch')
        analytics=json.loads((source/'analytics.json.partial').read_text())
        if analytics.get('source_sha256')!=j['source_sha256']:raise ValueError('analytics binding mismatch')
        root=self.hil.runtime/'data_previews';root.mkdir(exist_ok=True)
        final=root/j['episode_id'];stage=root/('.'+j['id']+'.partial')
        if final.exists():
            old=json.loads((final/'manifest.json').read_text())
            if old.get('source_signature')==j['source_signature'] and old.get('renderer')==PREVIEW_VERSION:
                j.update(status='ready',lease_until=0);self.save(j);return dict(ready=True)
            raise ValueError('existing preview requires explicit archival')
        stage.mkdir(exist_ok=True)
        for name in PREVIEW_FILES:
            # Keep upload artifacts until the directory transaction commits; retries are safe.
            dst=stage/name
            if dst.exists():dst.unlink()
            os.link(source/(name+'.partial'),dst)
        videos=[dict(camera=c,url=f"/api/data/video/{j['episode_id']}/{c}.mp4",offset_s=0,duration_s=timeline['duration_s']) for c in ('head','left','right')]
        atomic(stage/'manifest.json',dict(version=1,renderer=PREVIEW_VERSION,source_signature=j['source_signature'],source_sha256=j['source_sha256'],fps=30,videos=videos,files=supplied,notes=['30fps审阅时间轴；保持帧与断流记录在timeline.json，不作为训练对齐。']))
        stage.rename(final)
        j.update(status='ready',lease_until=0,finished_unix=time.time());self.save(j)
        return dict(ready=True)

    def finish(self,body):
        with self.lock:
            j=self.leased(body['id'],body.get('lease'))
            names=body.get('clips')
            if not isinstance(names,list) or sorted(names)!=sorted(c['name'] for c in j['clips']):raise ValueError('clip publication incomplete')
            j.update(status='ready',lease_until=0,finished_unix=time.time());self.save(j);return dict(job=j)
    def video(self,key,name):
        if not re.fullmatch(r'clip-[0-9]+\.mp4',name):raise ValueError('invalid video')
        j=self.load(key)
        if not any(c['name']==name for c in j['clips']):raise ValueError('unpublished clip')
        return self.root()/'videos'/key/name

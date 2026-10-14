#!/usr/bin/env python3
"""Server worker: authenticated pull, source hashes, encode, bounded return.
Config and credentials stay server-local. No robot SDK or command publisher.
"""
import argparse
import base64
import hashlib
import http.cookiejar
import json
from pathlib import Path
import threading
import time
from urllib.request import Request, build_opener, HTTPCookieProcessor, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
# Source checkouts use a sibling shared tree; release archives bundle it in src.
from pathlib import Path as _DaggerPath
import sys as _dagger_sys
_dagger_root = _DaggerPath(__file__).resolve().parents[1]
_dagger_sources = [_dagger_root / "src"]
if not (_dagger_root / "src/dagger_core").is_dir():
    _dagger_sources.append(_dagger_root.parent / "shared/src")
for _dagger_source in reversed(_dagger_sources):
    if _dagger_source.is_dir() and str(_dagger_source) not in _dagger_sys.path:
        _dagger_sys.path.insert(0, str(_dagger_source))

from f1dagger.interventions import intervention_windows, RENDER_VERSION
from dagger_core.review_video import render

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*a,**kw):raise ValueError('redirect forbidden')

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

class Client:
    def __init__(self,config):
        self.config=config;self.url=config['robot_url'].rstrip('/');p=urlsplit(self.url)
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.path or p.query or p.fragment:raise ValueError('invalid robot URL')
        self.origin=self.url;self.opener=build_opener(ProxyHandler({}),NoRedirect(),HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.post('/api/login',dict(token=Path(config['access_token_file']).read_text().strip()))
    def post(self,path,body):
        req=Request(self.url+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Origin':self.origin,'X-F1-Request':'1'})
        try:
            with self.opener.open(req,timeout=15) as r:result=json.load(r)
        except HTTPError as exc:
            detail=json.loads(exc.read(65536));raise ValueError(detail.get('error',str(exc))) from exc
        if not result.get('ok'):raise ValueError(result.get('error','request failed'))
        return result
    def op(self,op,job,**fields):return self.post('/api/hil/media/'+op,dict(id=job['id'],lease=job['lease'],**fields))
    def transfer(self,op,job,**fields):
        while True:
            try:return self.op(op,job,**fields)
            except ValueError as exc:
                if 'transfer_paused_active_session' not in str(exc):raise
                self.op('progress',job,status='returning' if op in ('upload','publish') else 'transferring');time.sleep(2)
    def download(self,job,name,path,start=0,length=None):
        path.parent.mkdir(parents=True,exist_ok=True)
        have=path.stat().st_size if path.exists() else 0
        with path.open('ab') as f:
            while length is None or have<length:
                n=min(131072,length-have) if length is not None else 131072
                result=self.transfer('source',job,name=name,offset=start+have,count=n)
                data=base64.b64decode(result['data'],validate=True)
                if not data:break
                f.write(data);have+=len(data)
                if start+have>=result['size']:break
        if length is not None and have!=length:raise ValueError('source truncated')
        return path
    def upload(self,job,path):
        offset=0
        while offset<path.stat().st_size:
            with path.open('rb') as f:f.seek(offset);data=f.read(131072)
            r=self.transfer('upload',job,name=path.name,offset=offset,data=base64.b64encode(data).decode());offset=r['offset']
            if offset>path.stat().st_size:raise ValueError('remote partial exceeds local source')


def process(client,job,cache):
    root=cache/job['id'];root.mkdir(parents=True,exist_ok=True)
    for name in ('journal/manifest.json','journal/observations.jsonl','journal/control.jsonl','journal/events.jsonl','payload/manifest.json','payload/frames.jsonl'):
        client.download(job,name,root/name)
    manifest=json.loads((root/'journal/manifest.json').read_text())
    if not manifest.get('complete') or sha(root/'journal/manifest.json')!=job['source_sha256']:raise ValueError('source manifest mismatch')
    for name in ('observations','control','events'):
        if sha(root/'journal'/f'{name}.jsonl')!=manifest['stream_sha256'][name]:raise ValueError('journal hash mismatch')
    for name in ('manifest.json','frames.jsonl'):
        if sha(root/'payload'/name)!=manifest['payload_sha256'][name]:raise ValueError('payload index hash mismatch')
    payload_manifest=json.loads((root/'payload/manifest.json').read_text())
    if not payload_manifest.get('complete') or payload_manifest.get('format')!='native-image-packs':
        raise ValueError('completed image pack required')
    rows=lambda p:[json.loads(x) for x in (root/p).read_text().splitlines() if x]
    obs=rows('journal/observations.jsonl');controls=rows('journal/control.jsonl');payload={r['observation_id']:r for r in rows('payload/frames.jsonl')}
    windows=intervention_windows(obs,controls,manifest['binding']['purpose']);published={x['name'] for x in job.get('clips',[])};names=[]
    stop=threading.Event();errors=[];phase=['transferring']
    def heartbeat():
        while not stop.wait(20):
            try:client.op('progress',job,status=phase[0])
            except Exception as exc:errors.append(exc);return
    thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    try:
        for window in windows:
            name=f'clip-{window["intervention_id"]}.mp4';names.append(name)
            if name in published:continue
            # Include one causal image before start, and no future context past end.
            candidates=[r for r in obs if r['available_monotonic_ns']<=window['end_ns']]
            before=[i for i,r in enumerate(candidates) if r['available_monotonic_ns']<=window['start_ns']]
            selected=candidates[before[-1] if before else 0:]
            for row in selected:
                archived=payload.get(row['observation_id'])
                if not archived:raise ValueError('clip lacks original images')
                for frame,original in zip(archived['frames'],row['frames']):
                    if any(frame[k]!=original[k] for k in ('camera','frame_id','host_received_monotonic_ns','timestamp')):raise ValueError('payload identity mismatch')
                    p=root/'images'/frame['payload_sha256']
                    source='payload/'+frame['camera']+'.pack'
                    if frame['camera'] not in ('head','left_wrist','right_wrist'):raise ValueError('camera mismatch')
                    if not p.exists() or p.stat().st_size!=frame['payload_bytes']:
                        client.download(job,source,p,frame['payload_offset'],frame['payload_bytes'])
                    if sha(p)!=frame['payload_sha256']:raise ValueError('frame hash mismatch')
            def image(row,frame):
                archive=next(f for f in payload[row['observation_id']]['frames'] if f['camera']==frame['camera'])
                return (root/'images'/archive['payload_sha256']).read_bytes()
            phase[0]='rendering';client.op('progress',job,status=phase[0])
            output=root/name;partial=root/(name+'.render.mp4')
            if partial.exists():partial.unlink() # only this job's disposable rendering artifact
            caption=f'{manifest.get("task_prompt","")} | {manifest["binding"].get("model_id")} | {manifest["binding"].get("model_version")}'
            timeline=render(window,selected,controls,image,partial,caption);partial.replace(output)
            meta=dict(window,source_sha256=job['source_sha256'],renderer=RENDER_VERSION,run_id=job['run_id'],model_id=manifest['binding'].get('model_id'),model_version=manifest['binding'].get('model_version'),task=manifest.get('task_prompt'),render_fps=30,source_time='host receive / available monotonic',timeline=timeline,training_ready=False)
            index=root/name.replace('.mp4','.json');index.write_text(json.dumps(meta,ensure_ascii=False))
            phase[0]='returning';client.op('progress',job,status=phase[0]);client.upload(job,index);client.upload(job,output)
            client.transfer('publish',job,name=name,bytes=output.stat().st_size,sha256=sha(output),index_sha256=sha(index))
            if errors:raise errors[0]
        client.op('finish',job,clips=names)
    finally:stop.set();thread.join(2)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',required=True);p.add_argument('--once',action='store_true');a=p.parse_args()
    config=json.loads(Path(a.config).read_text());cache=Path(config['cache']).resolve();cache.mkdir(parents=True,exist_ok=True)
    client=None
    while True:
        job=None
        try:
            if client is None:client=Client(config)
            job=client.post('/api/hil/media/claim',{}).get('job')
            if job:process(client,job,cache)
        except Exception as exc:
            print('video worker:',str(exc),flush=True)
            if job and client:
                try:client.op('progress',job,status='failed',error=str(exc))
                except Exception:client=None # expired lease is reclaimed after restart
            elif isinstance(exc,(OSError,URLError)):client=None
        if a.once:break
        time.sleep(5)

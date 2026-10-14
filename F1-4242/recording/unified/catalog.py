"""Bounded, read-only common capture catalogue and original-frame replay."""
from bisect import bisect_right
import hashlib
import json
import math
from pathlib import Path
import threading
from .contract import CAMERAS, SCHEMA, confined, sha256
from .capture import read_capture, read_image
from .storage import read_config


def identity(path): return hashlib.sha256(('f1_capture:'+str(path)).encode()).hexdigest()[:24]


class CaptureCatalog:
    def __init__(self, runtime):
        self.config = Path(runtime)/'storage.json'
        self.cache = None
        self.lock = threading.RLock()

    def roots(self):
        if not self.config.is_file(): return []
        c = read_config(self.config)
        return [Path(c['paths']['pico'])]

    def catalog(self):
        result = []
        for root in self.roots():
            if not root.is_dir(): continue
            for run in sorted(root.iterdir(),reverse=True)[:1000]:
                if not run.is_dir() or run.is_symlink() or run.name.startswith('.') or run.name.endswith('.partial'): continue
                try:
                    mpath = confined(run,'capture.json')
                    if mpath.stat().st_size > 1000000: continue
                    m = json.loads(mpath.read_text())
                    if m.get('schema') != SCHEMA or not m.get('committed') or not m.get('complete'): continue
                    files=[]
                    for name in ['capture.json',*m['files']]:
                        path=confined(run,name);st=path.stat()
                        files.append(dict(name=name,relative_path=name,size_bytes=st.st_size,modified_at=st.st_mtime))
                    eid=identity(run);digest=sha256(mpath)
                    sig=hashlib.sha256(json.dumps([digest,files],sort_keys=True).encode()).hexdigest()
                    result.append(dict(id=eid,name=run.name,path=str(run),relative_path=str(run),source='pico_capture',
                        format=SCHEMA,recording_status='committed',metadata_sha256=digest,source_signature=sig,
                        started_at=m.get('started_unix',mpath.stat().st_mtime),modified_at=mpath.stat().st_mtime,
                        duration_s=m['rows']/float(m['fps']),message_count=m['rows'],file_count=len(files),files=files,
                        size_bytes=sum(f['size_bytes'] for f in files),videos=[],topics=[],training_ready=False,
                        preview_status='raw_local',replay_url='/api/data/capture/'+eid+'/timeline'))
                except (OSError,ValueError,KeyError,TypeError): continue
        return result

    def datasets(self):
        if not self.config.is_file(): return []
        root=Path(read_config(self.config)['paths']['datasets'])
        if not root.is_dir(): return []
        result=[]
        for directory in sorted(root.iterdir(),reverse=True)[:1000]:
            if not directory.is_dir() or directory.is_symlink() or directory.name.endswith('.partial'):continue
            try:
                audit_path=confined(directory,'f1_audit.json')
                index_path=confined(directory,'f1_files.json')
                if audit_path.stat().st_size>1000000 or index_path.stat().st_size>8000000:continue
                audit=json.loads(audit_path.read_text());index=json.loads(index_path.read_text())
                if not audit.get('structure_verified') or not audit.get('reader_roundtrip_verified'):continue
                if len(index['files'])>10000:continue
                files=[]
                for rel in [*index['files'],'f1_files.json']:
                    p=confined(directory,rel);st=p.stat()
                    files.append(dict(name=p.name,relative_path=rel,size_bytes=st.st_size,modified_at=st.st_mtime))
                digest=sha256(index_path)
                signature=hashlib.sha256(json.dumps([digest,files],sort_keys=True).encode()).hexdigest()
                result.append(dict(id=identity(directory),name=directory.name,path=str(directory),relative_path=str(directory),
                    source='unified_dataset',format='official_lerobot_v3',metadata_sha256=digest,source_signature=signature,
                    duration_s=audit['frames']/audit['fps'],message_count=audit['frames'],dataset_episodes=audit['episodes'],
                    started_at=audit_path.stat().st_mtime,modified_at=audit_path.stat().st_mtime,
                    size_bytes=sum(f['size_bytes'] for f in files),file_count=len(files),files=files,topics=[],videos=[],
                    training_ready=bool(audit.get('training_ready')),release_status=audit.get('release_status')))
            except (OSError,ValueError,KeyError,TypeError):continue
        return result

    def index(self,eid):
        item=next((r for r in self.catalog() if r['id']==eid),None)
        if not item: raise ValueError('capture not found')
        if self.cache and self.cache['signature']==item['source_signature']: return self.cache
        run=Path(item['path']);index=confined(run,'samples.jsonl')
        if index.stat().st_size>256*1024*1024: raise ValueError('index too large; use offline tools')
        m,rows=read_capture(run,verify=False)
        if sha256(index)!=m['files']['samples.jsonl']: raise ValueError('capture index checksum differs')
        if not rows or len(rows)>120000: raise ValueError('invalid replay size')
        times=[(r['time_ns']-rows[0]['time_ns'])/1e9 for r in rows]
        if any(b<=a for a,b in zip(times,times[1:])): raise ValueError('capture clock regressed')
        self.cache=dict(run=run,rows=rows,times=times,signature=item['source_signature'])
        return self.cache

    def timeline(self,eid):
        with self.lock:
            c=self.index(eid);duration=c['times'][-1]
            return dict(frames=len(c['rows']),duration_s=duration,observed_hz=(len(c['rows'])-1)/duration if duration else None,
                cameras=list(CAMERAS),original_capture=True,playback_preview_fps=5,training_ready=False)

    def frame(self,eid,camera,timestamp):
        if camera not in CAMERAS or not math.isfinite(timestamp) or timestamp<0: raise ValueError('invalid camera/time')
        with self.lock:
            c=self.index(eid);i=max(0,bisect_right(c['times'],timestamp)-1)
            row=c['rows'][i]
            return read_image(c['run'],row['images'][camera]),dict(observation_id=i,time_s=c['times'][i])

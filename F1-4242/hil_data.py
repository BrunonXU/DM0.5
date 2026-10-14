"""Read-only F1 HIL catalog and indexed original-frame replay; no ROS imports."""
from bisect import bisect_right
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import threading

from hil_service import small_json

CAMERAS = ('head', 'left_wrist', 'right_wrist')

def roots(hil):
    c = small_json(hil.config_path)
    result = [Path(c['runtime']).expanduser()/'raw']
    legacy = c.get('legacy_data_roots', [])
    if not isinstance(legacy, list) or len(legacy) > 16 or any(not isinstance(p, str) for p in legacy):
        raise ValueError('legacy_data_roots must be a bounded list of paths')
    for value in ([c['data_root']] if c.get('data_root') else []) + legacy:
        base = Path(value).expanduser()
        result += [base/'demonstration', base/'policy_hil']
    # Keep old recordings readable after selecting a new write root. No moving,
    # merging, directory creation or write permission is implied by cataloguing.
    return list(dict.fromkeys(p for p in result if p.is_absolute() and not any(x.is_symlink() for x in (p, *p.parents))))


def find_run(hil, run_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', run_id):
        raise ValueError('invalid run id')
    found = [r/run_id for r in roots(hil) if (r/run_id).is_dir() and not (r/run_id).is_symlink()]
    if len(found) != 1:
        raise ValueError('session missing or ambiguous')
    return found[0]


def confined(run, name):
    p = run/name
    if p.is_symlink() or any(x.is_symlink() for x in p.parents) or not p.resolve().is_relative_to(run.resolve()):
        raise ValueError('recording path escaped')
    return p


def episode_id(run):
    return hashlib.sha256(('f1_hil:'+run.name).encode()).hexdigest()[:24]


def stamp(ns):
    return datetime.fromtimestamp(ns/1e9, timezone.utc).isoformat()


class HILData:
    def __init__(self, hil):
        self.hil = hil
        self.lock = threading.RLock()
        self.index_cache = None

    def catalog(self):
        result = []
        try:
            directories = roots(self.hil)
        except (OSError, ValueError, KeyError):
            return []
        seen = set()
        for root in directories:
            if not root.is_dir(): continue
            for run in sorted(root.iterdir(), reverse=True)[:500]:
                if not run.is_dir() or run.is_symlink() or run.name.startswith(".") or run.name in seen: continue
                try:
                    row = self.row(run)
                    if row:
                        result.append(row); seen.add(run.name)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        return sorted(result, key=lambda x:x['started_at'], reverse=True)

    def row(self, run):
        name = 'journal/manifest.json' if (run/'journal/manifest.json').exists() else 'journal.partial/manifest.json'
        mpath = confined(run, name); m = small_json(mpath); b = m.get('binding', {})
        if b.get('purpose') not in ('demonstration', 'policy_hil') or b.get('recording_mode') == 'inference-only': return None
        files = []
        # Fixed folders, no recursive scanning of model assets or image packs.
        for folder in ('journal', 'journal.partial', 'payload', 'payload.partial'):
            directory = confined(run, folder)
            if directory.is_dir():
                for p in sorted(directory.iterdir()):
                    p = confined(run, str(p.relative_to(run)))
                    if p.is_file():
                        st=p.stat(); files.append(dict(name=p.name,relative_path=str(p.relative_to(run)),size_bytes=st.st_size,modified_at=stamp(st.st_mtime_ns),mtime_ns=st.st_mtime_ns))
        ppath=confined(run,'payload/manifest.json')
        payload=small_json(ppath) if ppath.exists() else {}
        complete=bool(m.get('complete') and payload.get('complete') and not m.get('writer_error') and not payload.get('writer_error') and not m.get('drop_count') and not payload.get('drop_count') and m.get('counts',{}).get('observations')==payload.get('count'))
        sha=hashlib.sha256(mpath.read_bytes()).hexdigest()
        signature=hashlib.sha256(json.dumps([sha, files],sort_keys=True).encode()).hexdigest()
        row = dict(id=episode_id(run),name=run.name,run_id=run.name,path=str(run),relative_path=str(run),
            source='hil_'+b['purpose'],format='f1_native_image_packs',size_bytes=sum(x['size_bytes'] for x in files),file_count=len(files),files=files,
            started_at=stamp(m.get('started_wall_ns',mpath.stat().st_mtime_ns)),modified_at=stamp(mpath.stat().st_mtime_ns),
            duration_s=max(0,(m.get('closed_wall_ns',0)-m.get('started_wall_ns',0))/1e9),
            source_signature=signature,metadata_sha256=sha,recording_status='committed' if complete else 'recovery',
            observations=m.get('counts',{}).get('observations',0),capture_hz=b.get('capture_hz'),
            task=m.get('task_prompt',b.get('task','')),execution=b.get('execution_mode'),training_ready=False,
            replay_url=f'/api/data/hil/{episode_id(run)}/timeline' if complete else None,
            videos=[],topics=[],binding=b,preview_url=None)
        if complete:
            from hil_media import PREVIEW_VERSION
            key=hashlib.sha256((sha+PREVIEW_VERSION).encode()).hexdigest()
            row['preview_status']='waiting_server'
            try:row['preview_status']=small_json(Path(small_json(self.hil.config_path)['runtime']).expanduser()/'video-jobs'/(key+'.json'))['status']
            except (OSError,ValueError,KeyError):pass
            try:
                media=small_json(self.hil.runtime/'data_previews'/row['id']/'manifest.json')
                if media.get('source_signature')==signature and media.get('renderer')==PREVIEW_VERSION:
                    row.update(videos=media['videos'],analytics_url='/api/data/analytics/'+row['id'],preview_status='ready',duration_s=media['videos'][0]['duration_s'])
            except (OSError,ValueError,KeyError):pass
        return row

    def locate(self, eid):
        if not re.fullmatch('[a-f0-9]{24}',eid):raise ValueError('invalid episode id')
        row=next((r for r in self.catalog() if r['id']==eid),None)
        if row is None:raise ValueError('recording not found')
        if row['recording_status']!='committed':raise ValueError('recording not committed; retained for recovery')
        return Path(row['path']), row

    def index(self, eid):
        cached=self.index_cache
        try:
            if not cached or episode_id(cached['run'])!=eid:raise ValueError('new episode')
            run=cached['run'];row=self.row(run)
            if not row or row['recording_status']!='committed':raise ValueError('recording changed')
        except (OSError,ValueError,KeyError):
            run,row=self.locate(eid)
        if self.index_cache and self.index_cache['signature']==row['source_signature']:
            self.index_cache['run']=run
            return self.index_cache
        path=confined(run,'payload/frames.jsonl')
        if path.stat().st_size>256*1024*1024:raise ValueError('index exceeds replay limit; use offline export')
        manifest=small_json(confined(run,'journal/manifest.json'))
        entries=[];digest=hashlib.sha256();first=None
        with path.open('rb') as f:
            while True:
                offset=f.tell();line=f.readline(131073)
                if not line:break
                if len(line)>131072:raise ValueError('oversized frame index row')
                digest.update(line);item=json.loads(line);ns=item['capture_started_monotonic_ns']
                if first is None:first=ns
                if entries and ns<entries[-1]['ns']:raise ValueError('nonmonotonic recording')
                entries.append(dict(offset=offset,ns=ns,time_s=(ns-first)/1e9,observation_id=item['observation_id']))
                if len(entries)>120000:raise ValueError('recording exceeds replay limit')
        if digest.hexdigest()!=manifest.get('payload_sha256',{}).get('frames.jsonl') or len(entries)!=row['observations'] or not entries:raise ValueError('frame index integrity mismatch')
        value=dict(signature=row['source_signature'],run=run,entries=entries,times=[x['time_s'] for x in entries])
        self.index_cache=value
        return value

    def timeline(self,eid):
        with self.lock:
            index=self.index(eid);entries=index['entries'];duration=entries[-1]['time_s']
            return dict(frames=len(entries),duration_s=duration,observed_hz=(len(entries)-1)/duration if duration else None,
                        cameras=list(CAMERAS),original_capture=True,playback_preview_fps=5,
                        training_ready=False)

    def frame(self,eid,camera,timestamp):
        import math
        if camera not in CAMERAS or not math.isfinite(timestamp) or timestamp<0:raise ValueError('invalid camera/time')
        with self.lock:
            index=self.index(eid);i=max(0,bisect_right(index['times'],timestamp)-1);entry=index['entries'][i];run=index['run']
            with confined(run,'payload/frames.jsonl').open('rb') as f:
                f.seek(entry['offset']);item=json.loads(f.readline(131073))
            frame=next(x for x in item['frames'] if x['camera']==camera)
            size=frame['payload_bytes'];offset=frame.get('payload_offset')
            if frame.get('storage')!='f1-camera-pack-v1' or frame['path']!=camera+'.pack' or type(size) is not int or not 0<size<=8*1024*1024 or type(offset) is not int or offset<0:raise ValueError('invalid pack range')
            p=confined(run,'payload/'+frame['path'])
            if offset+size>p.stat().st_size:raise ValueError('truncated pack')
            with p.open('rb') as f:f.seek(offset);data=f.read(size)
            if hashlib.sha256(data).hexdigest()!=frame['payload_sha256']:raise ValueError('frame hash mismatch')
            return data,dict(observation_id=entry['observation_id'],time_s=entry['time_s'])

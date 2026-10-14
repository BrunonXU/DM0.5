"""Durable local conversion queue. Explicit worker invocation; no automatic upload."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
from .contract import atomic_json, sha256, confined
from .storage import read_config


def active_capture(proc_root='/proc'):
    """Fail closed when process inventory is unavailable. No process is stopped."""
    try: processes=list(Path(proc_root).iterdir())
    except OSError:return True
    for process in processes:
        if not process.name.isdigit() or process.name == str(os.getpid()):continue
        try:
            args=(process/'cmdline').read_bytes().split(b'\0')
            if b'f1dagger' in args or b'recording.worker' in args or b'pico_upper.supervisor' in args:return True
            if any(b'ros2' in a for a in args) and b'record' in args:return True
        except FileNotFoundError:continue
        except PermissionError:return True
    return False


def enqueue(queue_root, source, kind):
    source=Path(source).absolute()
    marker=confined(source,'capture.json' if kind=='pico' else 'journal/manifest.json')
    m=json.loads(marker.read_text())
    if (kind=='pico' and not m.get('committed')) or (kind!='pico' and not m.get('complete')):
        raise ValueError('only committed sources may be queued')
    digest=sha256(marker)
    job_id=hashlib.sha256((digest+'official-v3-r1').encode()).hexdigest()
    queue_root=Path(queue_root);confined(queue_root,'.');queue_root.mkdir(parents=True,exist_ok=True)
    path=queue_root/(job_id+'.json')
    # Exclusive creation preserves failed/retry/done status under concurrent scans.
    import uuid
    tmp=queue_root/('.'+job_id+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with tmp.open('x') as f:
            json.dump(dict(schema='f1_conversion_job_v1',id=job_id,source=str(source),source_kind=kind,
                marker=str(marker.relative_to(source)),source_sha256=digest,status='waiting_processor',
                attempts=0,created_unix=time.time(),training_ready=False),f)
            f.flush();os.fsync(f.fileno())
        try:os.link(tmp,path)
        except FileExistsError:pass
    finally:tmp.unlink(missing_ok=True)
    return path


def _scan(runtime):
    runtime=Path(runtime);c=read_config(runtime/'storage.json');found=[]
    for kind in ('pico','demonstration','policy_hil'):
        root=Path(c['paths'][kind])
        if not root.is_dir():continue
        for source in sorted(root.iterdir())[:10000]:
            if not source.is_dir() or source.is_symlink() or source.name.startswith('.') or source.name.endswith('.partial'):continue
            marker=source/('capture.json' if kind=='pico' else 'journal/manifest.json')
            if not marker.is_file():continue
            try:found.append(str(enqueue(runtime/'conversion-jobs',source,kind)))
            except (OSError,ValueError,KeyError):continue
    return found


def scan(runtime):
    q=Path(runtime)/'conversion-jobs';confined(q,'.');q.mkdir(parents=True,exist_ok=True)
    fd=os.open(q/'worker.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return []
        return _scan(runtime)
    finally:os.close(fd)


def run_one(runtime,lerobot_root,dagger_root, *, idle=active_capture):
    from .adapters import canonical,f710
    from .writer import export
    runtime=Path(runtime);c=read_config(runtime/'storage.json');scan(runtime)
    q=runtime/'conversion-jobs';q.mkdir(parents=True,exist_ok=True)
    with (q/'worker.lock').open('a+') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return dict(status='worker_busy')
        if idle():return dict(status='paused_capture_active')
        for path in sorted(q.glob('*.json')):
            job=json.loads(path.read_text())
            if job['status'] in ('done','failed'):continue
            source=Path(job['source']);marker=confined(source,job['marker'])
            try:
                if sha256(marker)!=job['source_sha256']:raise ValueError('source changed since enqueue')
                target=Path(c['paths']['datasets'])/job['id']
                # A crash after the atomic dataset rename is recoverable without re-encoding.
                if target.exists():
                    audit=json.loads(confined(target,'f1_audit.json').read_text())
                    if audit['source']['source_sha256']!=job['source_sha256'] or not audit.get('structure_verified'):
                        raise ValueError('destination exists but does not match verified source')
                else:
                    if target.with_name(target.name+'.partial').exists():
                        raise ValueError('interrupted export retained; inspect .partial before retry')
                    job.update(status='processing',attempts=job['attempts']+1);atomic_json(path,job)
                    bundle=canonical(source) if job['source_kind']=='pico' else f710(source,dagger_root)
                    audit=export(bundle,target,lerobot_root,cancel=idle)
                job.update(status='done',output=str(target),training_ready=False,completed_unix=time.time())
            except Exception as exc:
                job.update(status='failed',error=str(exc),training_ready=False)
            atomic_json(path,job);return job
    return dict(status='idle')


def main():
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('operation',choices=['scan','run-one','retry'])
    p.add_argument('--runtime',required=True);p.add_argument('--lerobot-root');p.add_argument('--dagger-root');p.add_argument('--job')
    a=p.parse_args()
    if a.operation=='scan':result=dict(jobs=scan(a.runtime))
    elif a.operation=='retry':
        if not a.job or len(a.job)!=64 or any(c not in '0123456789abcdef' for c in a.job):p.error('valid job ID required')
        path=confined(Path(a.runtime)/'conversion-jobs',a.job+'.json');job=json.loads(path.read_text())
        if job['status']!='failed':raise ValueError('only failed jobs may be retried')
        job.update(status='waiting_processor');atomic_json(path,job);result=job
    else:
        if not a.lerobot_root or not a.dagger_root:p.error('explicit dependency roots required')
        result=run_one(a.runtime,a.lerobot_root,a.dagger_root)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()

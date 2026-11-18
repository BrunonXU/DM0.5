"""Private stdio worker for read-only boot checks; owns no motion interfaces."""
import json
from pathlib import Path
import queue
import re
import threading

from .boot_capture import NativeBootSession, _json_file, capture_boot_recheck

PROTOCOL='f1-harness-boot-worker-v1'


def refresh_boot_site(site,*,session=None):
    """The same per-run validation for cold and reused discovery connections."""
    import hashlib
    from .development import validate_development
    site=Path(site).resolve()
    receipt=capture_boot_recheck(site,site,session=session)
    admission_path=site/'development.local.json'
    admission,_=_json_file(admission_path)
    admission['boot_recheck']=dict(path='boot-recheck.json',
        sha256=hashlib.sha256((site/'boot-recheck.json').read_bytes()).hexdigest())
    temporary=site/'.development.boot-recheck.tmp'
    temporary.write_text(json.dumps(admission,indent=2,ensure_ascii=False)+'\n')
    temporary.replace(admission_path)
    validated=validate_development(site/'owner.local.json')
    return dict(boot_recheck_complete=True,receipt=receipt,admission=validated,
                command_publishers=0,motion_authorized=False)


class BootRequests:
    def __init__(self,session,*,site_root=Path('/home/user/Bruno/HARNESS/site')):
        self.session=session;self.root=Path(site_root).resolve();self.finished={}

    def capture(self,body):
        if (not isinstance(body,dict) or set(body)!={'request_id','site','task_hash'}
                or not isinstance(body['request_id'],str) or not re.fullmatch('[a-f0-9]{32}',body['request_id'])
                or not isinstance(body['task_hash'],str) or not re.fullmatch('[a-f0-9]{64}',body['task_hash'])):
            raise ValueError('boot_request_binding_invalid')
        site=Path(body['site'])
        if (not site.is_absolute() or site.is_symlink() or site.parent!=self.root
                or site.resolve()!=site or not re.fullmatch('web-[a-f0-9]{32}',site.name)):
            raise ValueError('boot_request_requires_new_run_site')
        key=body['request_id'];binding=(str(site),body['task_hash'])
        if key in self.finished:
            previous,result=self.finished[key]
            if previous!=binding:raise ValueError('boot_request_id_rebound')
            return result
        admission,_=_json_file(site/'development.local.json')
        if admission.get('task_hash')!=body['task_hash']:raise ValueError('boot_request_task_changed')
        # Failed/unknown attempts are not recaptured over existing evidence.
        # A new run has its own immutable output paths and request identity.
        try:
            refresh_boot_site(site,session=self.session)
            result=dict(ok=True,request_id=key,site=str(site),task_hash=body['task_hash'])
        except Exception as exc:
            result=dict(ok=False,request_id=key,site=str(site),task_hash=body['task_hash'],
                        error=type(exc).__name__+':'+str(exc)[:1000])
        self.finished[key]=(binding,result)
        if len(self.finished)>32:self.finished.pop(next(iter(self.finished)))
        return result


def serve_boot_worker(template,*,stdin=None,stdout=None):
    import sys
    stdin=stdin or sys.stdin;stdout=stdout or sys.stdout
    profile,_=_json_file(Path(template)/'gateway.local.json')
    session=NativeBootSession(profile['observation']);requests=BootRequests(session)
    pending=queue.Queue(maxsize=1);ended=threading.Event()
    def emit(value):
        stdout.write(json.dumps(dict(protocol=PROTOCOL,**value),allow_nan=False)+'\n');stdout.flush()
    def read():
        try:
            while not ended.is_set():
                line=stdin.readline(65537)
                if not line or len(line)>65536:break
                body=json.loads(line)
                while not ended.is_set():
                    try:pending.put(body,timeout=.1);break
                    except queue.Full:pass
        finally:ended.set()
    thread=threading.Thread(target=read,daemon=True);thread.start()
    try:
        emit(dict(kind='ready',command_publishers=0,motion_authorized=False))
        while not ended.is_set():
            try:body=pending.get_nowait()
            except queue.Empty:
                session.spin_idle(.05);continue
            try:result=requests.capture(body)
            except Exception as exc:
                result=dict(ok=False,request_id=body.get('request_id') if isinstance(body,dict) else None,
                    error=type(exc).__name__+':'+str(exc)[:1000])
            emit(dict(kind='result',**result))
    finally:
        ended.set();session.close()

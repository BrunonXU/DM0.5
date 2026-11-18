"""Warm local numerical audit processes, never robot owners or code executors.

Uses existing bounded JSON framing and service credential filtering. There is
one request in flight per worker; failures/cancellation discard the entire pool
and all its result channels. Restart is explicit and creates a new generation.
This maintenance tool does not serialize the output smoother or authorize any
motion. Only trusted local SkillGeometry/ReturnGeometry sweep jobs are accepted.
"""
from copy import deepcopy
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

import numpy as np

from f1_harness.contracts import digest
from f1_harness.ipc import send,receive,MAX_MESSAGE
from f1_harness.site import _safe_environment
from sweep_blocks import check_block

PROTOCOL='f1-local-dense-audit-v1'
BINDING_FIELDS=('generation','batch_id','request_id','context_binding','job_sha256','block_index','stop_on_failure','asset_id')


class AuditCancelled(RuntimeError):pass
class AuditWorkerFailed(RuntimeError):pass


def _plain(value):
    if isinstance(value,np.ndarray):return value.tolist()
    if isinstance(value,np.generic):return value.item()
    if isinstance(value,dict):return {k:_plain(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [_plain(v) for v in value]
    return value


def pack_job(job):
    from f1_harness.skills import SkillGeometry
    from f1_harness.return_scene import ReturnGeometry
    full,scene,anchor,points=job
    if type(scene) not in (SkillGeometry,ReturnGeometry):raise ValueError('unsupported_audit_scene')
    if not 1<=len(points)<=64:raise ValueError('audit_block_requires_one_to_64_points')
    return _plain(dict(full=full,scene_type=type(scene).__name__,scene=asdict(scene),anchor=anchor,points=points))


def unpack_job(value):
    from f1_harness.skills import SkillGeometry
    from f1_harness.return_scene import ReturnGeometry
    kind={'SkillGeometry':SkillGeometry,'ReturnGeometry':ReturnGeometry}.get(value['scene_type'])
    if kind is None:raise ValueError('unsupported_audit_scene')
    scene=kind(**value['scene'])
    return value['full'],scene,value['anchor'],value['points']


def _validate_frame(value):
    size=len(json.dumps(value,allow_nan=False).encode())
    if size>MAX_MESSAGE:raise ValueError('audit_frame_exceeds_existing_ipc_limit')


def _bound_result(request,**values):
    return dict(protocol=PROTOCOL,kind='result',**{k:request[k] for k in BINDING_FIELDS},**values)


def serve(fd):
    """Dedicated numerical process; no owner, gateway, planner key or device IO."""
    from audit_worker_launch import verify_parent_death_signal
    verify_parent_death_signal()
    from f1_harness.numerical_service import NumericalDispatcher
    channel=socket.socket(fileno=fd)
    try:
        init=receive(channel)
        if init.get('protocol')!=PROTOCOL or init.get('kind')!='initialize':raise ValueError('invalid_audit_init')
        if digest(init['spec'])!=init['spec_sha256']:raise ValueError('audit_spec_identity_mismatch')
        assets=None
        if init.get('assets') is not None:
            from audit_assets import load_assets
            assets=load_assets(init['assets']['root'],expected_id=init['assets']['asset_id'])
            assets.verify_spec(init['spec'])
        spec=deepcopy(init['spec'])
        spec['gate']['runtime']=str(Path(spec['gate']['runtime'])/('audit-'+init['generation'])/str(init['worker_index']))
        numeric=NumericalDispatcher(spec)
        if assets is not None and not assets.unchanged():raise ValueError('numerical_assets_changed_during_start')
        send(channel,dict(protocol=PROTOCOL,kind='ready',generation=init['generation'],
            spec_sha256=init['spec_sha256'],worker_index=init['worker_index'],pid=os.getpid(),
            asset_id=None if assets is None else assets.asset_id))
        while True:
            request=receive(channel)
            if (request.get('protocol')!=PROTOCOL or request.get('kind')!='audit'
                    or request.get('generation')!=init['generation']
                    or request.get('asset_id')!=(None if assets is None else assets.asset_id)
                    or digest(request['job'])!=request.get('job_sha256')):
                raise ValueError('invalid_audit_request')
            try:
                if type(request.get('stop_on_failure')) is not bool:raise ValueError('invalid_audit_stop_mode')
                if assets is not None and not assets.unchanged():raise ValueError('numerical_assets_changed')
                result=check_block(numeric,unpack_job(request['job']),stop_on_failure=request['stop_on_failure'])
                if assets is not None and not assets.unchanged():raise ValueError('numerical_assets_changed')
                send(channel,_bound_result(request,ok=True,result=result))
            except Exception as exc:
                # Do not send arbitrary native exception strings or call stacks.
                send(channel,_bound_result(request,ok=False,error=type(exc).__name__))
                return  # state after a numerical error is not reused
    finally:channel.close()


class AuditPool:
    def __init__(self,spec,runtime,*,workers=2,python=None,assets=None,_worker_command=None):
        if type(workers) is not int or not 1<=workers<=2:raise ValueError('audit_workers_must_be_one_or_two')
        self.assets=None
        if assets is not None:
            from audit_assets import load_assets
            self.assets=load_assets(assets)
            spec=self.assets.relocate(spec)
        self.asset_id=None if self.assets is None else self.assets.asset_id
        self.asset_validation_mode=None if self.assets is None else self.assets.validation_mode
        self.spec=deepcopy(spec);self.spec_hash=digest(self.spec);self.runtime=Path(runtime)
        self.worker_count=workers;self.python=str(python or sys.executable)
        self.command=_worker_command or [self.python,str(Path(__file__).resolve()),'--worker']
        self.children=[];self.generation=None;self.ready=False;self.lock=threading.Lock()
        self.last_cleanup=[];self.startup_s=None

    def _check(self,deadline,cancelled,current):
        if cancelled():raise AuditCancelled('audit_cancelled')
        if not current():raise AuditCancelled('audit_context_invalidated')
        if time.monotonic()>=deadline:raise TimeoutError('audit_compute_deadline')

    def start(self,*,timeout_s=30.,cancelled=lambda:False):
        if not self.lock.acquire(blocking=False):raise AuditWorkerFailed('audit_pool_busy')
        starting=False
        try:
            if self.children:raise AuditWorkerFailed('audit_pool_requires_explicit_close_before_start')
            if self.assets is not None and not self.assets.unchanged():raise AuditWorkerFailed('numerical_assets_changed')
            if threading.current_thread() is not threading.main_thread():
                raise AuditWorkerFailed('audit_pool_start_requires_main_thread')
            if sys.platform!='linux':raise AuditWorkerFailed('audit_pool_parent_lifecycle_requires_linux')
            if self.assets is not None and self.assets.watch.fd is None:
                from audit_assets import load_assets
                self.assets=load_assets(self.assets.root,expected_id=self.asset_id)
                self.asset_validation_mode=self.assets.validation_mode
            if not math.isfinite(timeout_s) or timeout_s<=0:raise ValueError('invalid_startup_timeout')
            started=time.monotonic();deadline=started+timeout_s
            self._check(deadline,cancelled,lambda:True)
            starting=True
            self.generation=uuid.uuid4().hex;self.ready=False
            self.runtime.mkdir(parents=True,exist_ok=True)
            env=_safe_environment();root=Path(__file__).resolve().parents[1]
            control=((root/'control/src',) if (root/'control/src').is_dir() else
                     (root.parent/'DAgger/F1/src',root.parent/'DAgger/shared/src'))
            env['PYTHONPATH']=os.pathsep.join(str(x) for x in (root/'src',*control,root/'.overlay',root/'third_party/RPent'))
            env['OPENBLAS_NUM_THREADS']=env['OMP_NUM_THREADS']='1'
            env['PYTHONDONTWRITEBYTECODE']='1'
            for index in range(self.worker_count):
                self._check(deadline,cancelled,lambda:True)
                parent,child=socket.socketpair(socket.AF_UNIX,socket.SOCK_STREAM)
                parent.settimeout(.2)
                log=(self.runtime/(self.generation+'-'+str(index)+'.log')).open('wb')
                try:
                    launcher=Path(__file__).with_name('audit_worker_launch.py')
                    process=subprocess.Popen([self.python,str(launcher),str(os.getpid()),'--',
                        *self.command,str(child.fileno())],pass_fds=(child.fileno(),),
                        stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
                        env=env,start_new_session=True)
                except BaseException:
                    parent.close();log.close();raise
                finally:child.close()
                row=dict(process=process,channel=parent,log=log,index=index)
                self.children.append(row)
                send(parent,dict(protocol=PROTOCOL,kind='initialize',generation=self.generation,
                    worker_index=index,spec=self.spec,spec_sha256=self.spec_hash,
                    assets=None if self.assets is None else dict(root=str(self.assets.root),asset_id=self.asset_id)))
            with selectors.DefaultSelector() as selector:
                for row in self.children:selector.register(row['channel'],selectors.EVENT_READ,row)
                pending=len(self.children)
                while pending:
                    self._check(deadline,cancelled,lambda:True)
                    for key,_ in selector.select(.025):
                        row=key.data;reply=receive(row['channel'])
                        expected=dict(protocol=PROTOCOL,kind='ready',generation=self.generation,
                            spec_sha256=self.spec_hash,worker_index=row['index'],pid=row['process'].pid,asset_id=self.asset_id)
                        if reply!=expected:raise AuditWorkerFailed('audit_ready_identity_mismatch')
                        selector.unregister(row['channel']);pending-=1
                    if any(row['process'].poll() is not None for row in self.children):
                        raise AuditWorkerFailed('audit_worker_exited_during_start')
            self._check(deadline,cancelled,lambda:True)
            self.ready=True;self.startup_s=time.monotonic()-started
            return self
        except BaseException:
            if starting:self._close_owned()
            raise
        finally:self.lock.release()

    def map(self,jobs,*,context_binding,timeout_s=10.,cancelled=lambda:False,current=lambda:True,
            stop_on_failure=False):
        if not self.lock.acquire(blocking=False):raise AuditWorkerFailed('audit_pool_busy')
        try:
            if not self.ready or not self.children:raise AuditWorkerFailed('audit_pool_not_ready')
            if self.assets is not None and not self.assets.unchanged():raise AuditWorkerFailed('numerical_assets_changed')
            if type(stop_on_failure) is not bool:raise ValueError('invalid_audit_stop_mode')
            if not isinstance(context_binding,str) or len(context_binding)!=64:raise ValueError('audit_context_digest_required')
            if not math.isfinite(timeout_s) or timeout_s<=0:raise ValueError('invalid_audit_timeout')
            if not 1<=len(jobs)<=1024:raise ValueError('bounded_nonempty_audit_jobs_required')
            deadline=time.monotonic()+timeout_s;batch=uuid.uuid4().hex
            packets=[]
            for index,job in enumerate(jobs):
                self._check(deadline,cancelled,current)
                encoded=pack_job(job)
                packet=dict(protocol=PROTOCOL,kind='audit',generation=self.generation,
                    batch_id=batch,request_id=uuid.uuid4().hex,context_binding=context_binding,
                    block_index=index,job_sha256=digest(encoded),job=encoded,stop_on_failure=stop_on_failure,asset_id=self.asset_id)
                _validate_frame(packet);packets.append(packet)
            results=[None]*len(packets);next_index=0;pending={}
            with selectors.DefaultSelector() as selector:
                for row in self.children:selector.register(row['channel'],selectors.EVENT_READ,row)
                while next_index<len(packets) or pending:
                    self._check(deadline,cancelled,current)
                    for row in self.children:
                        if row['process'].poll() is not None:raise AuditWorkerFailed('audit_worker_exited')
                        if row['index'] not in pending and next_index<len(packets):
                            packet=packets[next_index];send(row['channel'],packet)
                            pending[row['index']]=packet;next_index+=1
                    for key,_ in selector.select(.025):
                        row=key.data;reply=receive(row['channel']);packet=pending.get(row['index'])
                        self._check(deadline,cancelled,current)
                        if (packet is None or reply.get('protocol')!=PROTOCOL or reply.get('kind')!='result'
                                or any(reply.get(k)!=packet[k] for k in BINDING_FIELDS)):
                            raise AuditWorkerFailed('audit_reply_binding_mismatch')
                        if reply.get('ok') is not True:raise AuditWorkerFailed('audit_native_worker_failed')
                        value=reply['result']
                        count=value.get('samples');failures=value.get('failures')
                        expected=len(jobs[packet['block_index']][3])
                        prefix=(stop_on_failure and value.get('stopped_on_first_failure') is True
                            and type(count) is int and 1<=count<=expected
                            and isinstance(failures,list) and len(failures)==1)
                        if (not isinstance(failures,list) or type(count) is not int
                                or not (count==expected or prefix)):
                            raise AuditWorkerFailed('audit_incomplete_result')
                        results[packet['block_index']]=dict(value,block_index=packet['block_index'])
                        del pending[row['index']]
            self._check(deadline,cancelled,current)
            if self.assets is not None and not self.assets.unchanged():raise AuditWorkerFailed('numerical_assets_changed')
            if any(v is None for v in results):raise AuditWorkerFailed('audit_incomplete_batch')
            return results
        except BaseException:
            self._close_owned();raise
        finally:self.lock.release()

    def _close_owned(self):
        self.ready=False;rows,self.children=self.children,[]
        if self.assets is not None:self.assets.close()
        for row in rows:
            row['channel'].close()
            try:os.killpg(row['process'].pid,signal.SIGTERM)
            except ProcessLookupError:pass
        until=time.monotonic()+.25
        for row in rows:
            try:row['process'].wait(timeout=max(.001,until-time.monotonic()))
            except subprocess.TimeoutExpired:pass
            # Reap this owned group even if its leader exited first.
            try:os.killpg(row['process'].pid,signal.SIGKILL)
            except ProcessLookupError:pass
            row['process'].wait(timeout=2);row['log'].close()
        self.last_cleanup=[dict(pid=row['process'].pid,returncode=row['process'].returncode) for row in rows]

    def close(self):
        if not self.lock.acquire(blocking=False):raise AuditWorkerFailed('audit_pool_busy_use_cancel_event')
        try:self._close_owned()
        finally:self.lock.release()

    def __enter__(self):return self.start()
    def __exit__(self,*args):self.close()


if __name__=='__main__':
    if len(sys.argv)!=3 or sys.argv[1]!='--worker':raise SystemExit('internal numerical worker only')
    serve(int(sys.argv[2]))

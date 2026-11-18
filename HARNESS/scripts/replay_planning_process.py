"""One offline plan-service process, using existing bounded JSON IPC.

No owner, ROS, device or model API is created. The caller uses a fake robot
and explicitly maps its sampling clock to wall time; clocks are never frozen
during computation. Lifecycle and credential filtering reuse existing helpers.
"""
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid

from f1_harness.contracts import digest, HarnessError, Task
from f1_harness.ipc import send, receive
from f1_harness.site import _safe_environment

PROTOCOL = 'f1-offline-wall-planner-v1'
METHODS = ('plan_skill', 'revalidate_plan', 'check_waypoint', 'geometry_context')


class PlanningProcess:
    WORKER_NAME = 'replay_planning_process.py'
    METHODS = METHODS
    def __init__(self, *, task, fixture, spec, runtime, clock_mapping):
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError('offline_planner_start_requires_main_thread')
        self.clock_mapping=clock_mapping;self.generation=uuid.uuid4().hex
        self.lock=threading.Lock();self.process=None;self.channel=None;self.log=None
        self.cleanup=None;self.sequence=0
        root=Path(__file__).resolve().parents[1]
        runtime=Path(runtime);runtime.mkdir(parents=True,exist_ok=False)
        env=_safe_environment()
        control=((root/'control/src',) if (root/'control/src').is_dir() else
                 (root.parent/'DAgger/F1/src',root.parent/'DAgger/shared/src'))
        env['PYTHONPATH']=os.pathsep.join(str(p) for p in (root/'src',*control,
            root/'.overlay',root/'third_party/RPent'))
        env['OPENBLAS_NUM_THREADS']=env['OMP_NUM_THREADS']='1'
        env['PYTHONDONTWRITEBYTECODE']='1'
        parent,child=socket.socketpair(socket.AF_UNIX,socket.SOCK_STREAM)
        self.channel=parent;parent.settimeout(15.)
        self.log=(runtime/'worker.log').open('wb')
        init=dict(task=task.model_dump(),fixture=fixture,spec=spec)
        self.initialization_sha256=digest(init)
        begin=time.monotonic()
        try:
            launcher=Path(__file__).with_name('audit_worker_launch.py')
            self.process=subprocess.Popen([sys.executable,str(launcher),str(os.getpid()),'--',
                sys.executable,str(Path(__file__).with_name(self.WORKER_NAME).resolve()),'--worker',str(child.fileno())],
                pass_fds=(child.fileno(),),stdin=subprocess.DEVNULL,stdout=self.log,stderr=subprocess.STDOUT,
                env=env,start_new_session=True)
            child.close()
            send(parent,dict(protocol=PROTOCOL,generation=self.generation,initialization=init,
                initialization_sha256=self.initialization_sha256))
            ready=receive(parent)
            if ready!=dict(protocol=PROTOCOL,generation=self.generation,pid=self.process.pid,
                          initialization_sha256=self.initialization_sha256,ready=True):
                raise RuntimeError('offline_planner_ready_identity_mismatch')
            self.startup_s=time.monotonic()-begin
            # Match the production owner -> plan-service RPC, which may try
            # several zero-publication candidates under one skill budget.
            parent.settimeout(92.)
        except BaseException:
            child.close();self.close();raise

    def dispatch(self, method, kwargs):
        if method not in self.METHODS:raise ValueError('offline_planner_method_forbidden')
        if not self.lock.acquire(blocking=False):raise RuntimeError('offline_planner_busy')
        try:
            if self.cleanup is not None:raise RuntimeError('offline_planner_closed')
            self.sequence+=1
            mapping=self.clock_mapping()
            request=dict(protocol=PROTOCOL,generation=self.generation,sequence=self.sequence,
                method=method,kwargs=kwargs,clock_mapping=mapping)
            key=digest(request)
            send(self.channel,dict(request=request,sha256=key))
            response=receive(self.channel)
            if (response.get('protocol')!=PROTOCOL or response.get('generation')!=self.generation
                    or response.get('sequence')!=self.sequence or response.get('request_sha256')!=key):
                raise RuntimeError('offline_planner_result_identity_mismatch')
            if not response['ok']:raise HarnessError(response['error'])
            return response['result']
        finally:self.lock.release()

    def close(self):
        if self.cleanup is not None:return
        if self.channel is not None:
            try:self.channel.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            self.channel.close()
        process=self.process
        if process is not None:
            try:process.wait(timeout=2.)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:process.wait(timeout=1.)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=1.)
        if self.log is not None:self.log.close()
        self.cleanup=dict(pid=None if process is None else process.pid,
            returncode=None if process is None else process.returncode,reaped=process is None or process.poll() is not None)


def serve(fd, *, service_wrapper=None, methods=METHODS, error_codes=()):
    from audit_worker_launch import verify_parent_death_signal
    verify_parent_death_signal()
    from f1_harness.numerical_service import NumericalDispatcher
    from f1_harness.skill_services import PlanService
    from f1_harness.owner_bridge import numerical_failure_detail
    import json
    channel=socket.socket(fileno=fd)
    try:
        init=receive(channel);data=init['initialization'];generation=init['generation']
        if init['protocol']!=PROTOCOL or digest(data)!=init['initialization_sha256']:
            raise ValueError('offline_planner_initialization_mismatch')
        numeric=NumericalDispatcher(data['spec'])
        mapping={}
        def now():return mapping['origin_ns']+time.monotonic_ns()-mapping['start_wall_ns']
        class Direct:
            def call(self,method,**kwargs):return numeric.dispatch(method,kwargs)
        service=PlanService(Task.model_validate(data['task']),data['fixture'],Path('/unused-offline.sock'),clock=now)
        service.numerics=Direct()
        if service_wrapper is not None:service=service_wrapper(service,numeric)
        send(channel,dict(protocol=PROTOCOL,generation=generation,pid=os.getpid(),
            initialization_sha256=init['initialization_sha256'],ready=True))
        sequence=0
        while True:
            try:envelope=receive(channel)
            except ConnectionError:break
            request=envelope['request']
            if (digest(request)!=envelope['sha256'] or request.get('protocol')!=PROTOCOL
                    or request.get('generation')!=generation or request.get('sequence')!=sequence+1
                    or request.get('method') not in methods):
                raise ValueError('offline_planner_request_identity_mismatch')
            sequence+=1;mapping=request['clock_mapping']
            if any(type(mapping.get(k)) is not int or mapping[k]<0 for k in ('origin_ns','start_wall_ns')):
                raise ValueError('offline_planner_clock_mapping_invalid')
            response=dict(protocol=PROTOCOL,generation=generation,sequence=sequence,request_sha256=envelope['sha256'])
            try:response.update(ok=True,result=service.dispatch(request['method'],request['kwargs']))
            except Exception as exc:
                detail=numerical_failure_detail(exc)
                # Only explicitly registered exact messages cross this boundary;
                # arbitrary native exception strings retain the existing filter.
                if isinstance(exc,HarnessError) and str(exc) in error_codes:
                    detail=dict(error_type=type(exc).__name__,code=str(exc),metrics={})
                response.update(ok=False,error=detail['code']+':'+json.dumps(dict(diagnostic=detail),separators=(',',':')))
            send(channel,response)
    finally:channel.close()


if __name__=='__main__':
    if len(sys.argv)!=3 or sys.argv[1]!='--worker':raise SystemExit('internal offline planner only')
    serve(int(sys.argv[2]))

"""Supervisor-owned read-only SSH worker, isolated from task/control authority."""
import json
from pathlib import Path
import queue
import shlex
import subprocess
import threading
import time
import uuid
from .boot_worker import PROTOCOL

ROS_SETUPS=('/opt/ros/jazzy/setup.bash','/home/user/workspace/f1/install/setup.bash',
    '/home/user/workspace/agv/install/setup.bash',
    '/home/user/workspace/camera/OrbbecSDK_ROS2/install/setup.bash',
    '/home/user/workspace/camera/ros2_ws/install/setup.bash')


class WarmBootClient:
    def __init__(self,config,runtime):
        self.config=config;self.runtime=Path(runtime);self.process=None
        self.ready=threading.Event();self.ended=threading.Event();self.responses=queue.Queue(maxsize=2)
        self.lock=threading.Lock();self.log=None;self.reader=None

    def start(self):
        from .collect import ssh_reader_command
        from .site import _safe_environment
        self.runtime.mkdir(parents=True,exist_ok=True)
        self.log=(self.runtime/'boot-worker.log').open('a',buffering=1)
        cfg=self.config;identity=uuid.uuid4().hex
        template='/home/user/Bruno/HARNESS/site/'+Path(cfg['local_site']).name
        argv=['/home/user/Bruno/DAgger-runtime/f1-09-venv/bin/python',
            cfg['remote_bundle']+'/native/entry.py','--run',
            '/home/user/Bruno/HARNESS/runs/boot-worker-'+identity,'boot-worker',template]
        command=''.join('source '+shlex.quote(p)+' || exit\n' for p in ROS_SETUPS)
        command+='export RMW_IMPLEMENTATION=rmw_fastrtps_cpp\n'
        command+='exec '+shlex.join(argv)
        ssh=ssh_reader_command({},host=cfg['host'],port=cfg['port'],identity=cfg['identity'],known_hosts=cfg['known_hosts'])[:-1]
        try:
            self.process=subprocess.Popen(ssh+[shlex.join(['bash','-c',command])],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,text=True,
                bufsize=1,start_new_session=True,env=_safe_environment())
        except BaseException:
            self.log.close();raise
        self.reader=threading.Thread(target=self._read,daemon=True);self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                if self.log is not None and not self.log.closed:self.log.write(line)
                try:value=json.loads(line)
                except ValueError:continue
                if not isinstance(value,dict) or value.get('protocol')!=PROTOCOL:continue
                if value.get('kind')=='ready':
                    if value.get('command_publishers')!=0 or value.get('motion_authorized') is not False:
                        break
                    self.ready.set()
                elif value.get('kind')=='result':
                    try:self.responses.put_nowait(value)
                    except queue.Full:break
        finally:
            self.ended.set()
            if self.log is not None:self.log.close()

    def capture(self,site,task_hash,stop):
        if not self.lock.acquire(blocking=False):raise RuntimeError('boot_worker_request_busy')
        try:
            # Cold fallback is permitted only before dispatch. Once sent, a
            # timeout/unknown outcome never starts a second writer for this site.
            if self.process is None or self.process.poll() is not None or self.ended.is_set():
                return False
            wait_until=time.monotonic()+1.
            while not self.ready.is_set() and time.monotonic()<wait_until:
                if stop.wait(.05):raise InterruptedError('已取消启动复核。')
                if self.ended.is_set():return False
            if self.process is None or self.process.poll() is not None or not self.ready.is_set() or self.ended.is_set():
                return False
            request=dict(request_id=uuid.uuid4().hex,site=site,task_hash=task_hash)
            try:
                self.process.stdin.write(json.dumps(request)+'\n');self.process.stdin.flush()
            except (OSError,ValueError) as exc:
                self.close();raise RuntimeError('boot_worker_dispatch_unknown') from exc
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                if stop.is_set():self.close();raise InterruptedError('已取消启动复核。')
                try:value=self.responses.get(timeout=.1)
                except queue.Empty:
                    if self.ended.is_set():
                        self.close();raise RuntimeError('boot_worker_result_unknown')
                    continue
                if any(value.get(k)!=v for k,v in request.items()):
                    self.close();raise RuntimeError('boot_worker_reply_binding_mismatch')
                if value.get('ok') is not True:raise RuntimeError('boot_worker_recheck_failed:'+str(value.get('error'))[:1000])
                return True
            self.close();raise TimeoutError('boot_worker_result_unknown')
        finally:self.lock.release()

    def close(self):
        process=self.process
        if process is not None and process.poll() is None:
            try:process.stdin.close()
            except OSError:pass
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:process.wait(timeout=2)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
        self.ended.set()
        if self.reader is not None:self.reader.join(timeout=1)
        if self.log is not None and (self.reader is None or not self.reader.is_alive()):self.log.close()

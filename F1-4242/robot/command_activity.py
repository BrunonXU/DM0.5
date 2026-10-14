"""Actual RMW command activity, including idle peers. Not a hardware arbiter."""
import time,threading
from .command_monitor_bridge import CommandMonitor,publisher_gid

class CommandActivity:
    def __init__(self,profile,name,clock=time.monotonic,monitor_factory=CommandMonitor):
        self.clock=clock;self.lock=threading.RLock();self.own={};self.external={};self.armed={};self.conflicts=set();self.error=None
        cfg=dict(profile,topics={**profile['topics'],'arms_command':profile['topics']['joint_command']})
        self.monitor=monitor_factory(cfg,name,self.record)
        self.started=clock();self.observation_s=profile.get('graph_observation_s',1.);self.quiet_s=.5
    def record(self,key,gid):
        key='joint_command' if key=='arms_command' else key
        now=self.clock()
        if not gid or not any(bytes.fromhex(gid)):raise RuntimeError('命令来源 GID 无效')
        if self.own.get(gid,0)>now:return
        self.external[(key,gid)]=now
        if key in self.armed:self.conflicts.add(key)
    def poll(self):
        try:self.monitor.poll()
        except Exception as exc:self.error=str(exc)
        if self.error:raise RuntimeError(self.error)
        if self.monitor.started_ns is None or self.clock()-self.started<self.observation_s:raise RuntimeError('实际命令监听仍在观察')
        now=self.clock();self.external={k:t for k,t in self.external.items() if now-t<10};self.own={g:t for g,t in self.own.items() if t>now}
    def check(self,key):
        with self.lock:
            self.poll()
            if key in self.conflicts:raise RuntimeError('执行期间检测到其他控制源发出命令，已中止本次输出')
            if any(k==key and self.clock()-t<=self.quiet_s for (k,g),t in self.external.items()):raise RuntimeError('检测到其他控制源正在发送命令，请松开原手柄并结束原动作')
    def bind(self,gid):
        with self.lock:self.own[gid]=float('inf')
    def arm(self,key):
        with self.lock:self.check(key);self.armed[key]=True
    def release(self,key,gid=None):
        with self.lock:
            # Drain pending commands before disarming; never discard active conflicts early.
            try:self.poll()
            except RuntimeError:pass
            self.armed.pop(key,None);self.conflicts.discard(key)
            if gid:self.own[gid]=self.clock()+2
    def snapshot(self):
        with self.lock:
            error=None
            try:self.poll()
            except RuntimeError as exc:error=str(exc)
            now=self.clock()
            return {'ready':error is None,'error':error,'policy':'actual_commands','quiet_s':self.quiet_s,'active':{key:any(k==key and now-t<=self.quiet_s for (k,g),t in self.external.items()) or key in self.conflicts for key in ('joint_command','left_gripper_command','right_gripper_command','base_command')}}
    def close(self):self.monitor.close()

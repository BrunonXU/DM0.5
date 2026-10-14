"""One explicitly confirmed Pico session. Never loaded by GET or boot service."""
import argparse,codecs,fcntl,hashlib,json,math,os,pty,secrets,select,signal,socket,subprocess,time,urllib.request
from pathlib import Path
from core import atomic_json
from teleop_service import UNIT,legacy_config
from project_paths import installation_environment, read_profile, teleop_config

ROOT=Path(__file__).resolve().parents[1]
PROMPT='按 Enter 继续，Ctrl+C 取消。'

def proc_args(pid):
    return (Path('/proc')/str(pid)/'cmdline').read_bytes().split(b'\0')

def start_ticks(pid):return (Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()[19]

def markers(args):
    result=set()
    if b'pico_upper.supervisor' in args:result.add('upper')
    if any(x.endswith(b'all_remote_lxl.py') for x in args):result.add('lxl_bridge')
    if any(x.endswith(b'teleop_lxl.py') for x in args):result.add('lxl_teleop')
    if any(x.endswith(b'RoboticsServiceProcess') for x in args):result.add('pcservice')
    return result

def observed():
    rows={}
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():continue
        try:
            kinds=markers(proc_args(p.name))
            if kinds:rows[int(p.name)]=kinds
        except OSError:pass
    return rows

def check_hardware():
    with urllib.request.urlopen(installation_environment()['F1_CONSOLE_URL']+'/api/system',timeout=4) as response:system=json.load(response)
    if not system.get('contract',{}).get('ok'):raise ValueError('本体启动合同未通过；请先启动基础服务')
    with urllib.request.urlopen(installation_environment()['F1_CONSOLE_URL']+'/api/state',timeout=4) as response:state=json.load(response)
    for capability in ('joints','gripper'):
        value=state.get('capabilities',{}).get(capability,{})
        if not value.get('available'):
            peers=state.get('graph',{}).get('joint_command' if capability=='joints' else 'left_gripper_command',{}).get('publishers',[])
            names=', '.join(str(p.get('name','unknown')) for p in peers)
            raise ValueError((value.get('reason') or '双臂/夹爪不可用')+('：'+names if names else ''))
    if not isinstance(state.get('graph_age_s'),(int,float)) or not 0<=state['graph_age_s']<=.5:raise ValueError('控制发布者状态过期')
    if state.get('graph',{}).get('right_gripper_command',{}).get('publishers'):raise ValueError('右夹爪已有控制发布者')
    required=[f'arm_{side}_j{i}' for side in ('l','r') for i in range(1,8)]+[f'head_j{i}' for i in range(1,4)]+[f'body_j{i}' for i in range(1,6)]
    for value in [*(state.get('joints',{}).get(name,{}) for name in required),*(state.get('grippers',{}).get(side,{}) for side in ('left','right'))]:
        if not isinstance(value.get('age_s'),(int,float)) or not 0<=value['age_s']<=.25:raise ValueError('完整关节/夹爪反馈未就绪')
    return state

class Session:
    def __init__(self,folder):
        self.folder=Path(folder);self.request=json.loads((self.folder/'request.json').read_text());self.variant=self.request['variant']
        self.cfg=teleop_config()[self.variant]
        self.state={'id':self.folder.name,'variant':self.variant,'phase':'preparing','pid':os.getpid(),'process_start':start_ticks(os.getpid()),'created_unix':time.time(),'nonce':None,'error':None,'pcservice_owned':False}
        self.master=None;self.child=None;self.stopping=False;self.control_lock=None;self.text='';self.decoder=codecs.getincrementaldecoder('utf-8')('replace')
        self.log=(self.folder/'session.log').open('a',buffering=1)
        self.native_log=Path(installation_environment()['PICO_UPPER_RUNTIME'])/'logs/teleop.log'
        self.native_log_start=self.native_log.stat().st_size if self.native_log.exists() else 0
        self.last_publish=0;self.last_phase=None
        line=next(line for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
        self.cgroup=Path('/sys/fs/cgroup')/line.split('::',1)[1].lstrip('/')
        if self.cgroup.name!=UNIT:raise ValueError('遥操必须运行在独立固定systemd进程组')
    def publish(self):
        now=time.monotonic()
        if self.last_phase==self.state['phase'] and now-self.last_publish<.4:return
        self.last_phase=self.state['phase'];self.last_publish=now
        self.state['checked_unix']=time.time();atomic_json(self.folder/'status.json',self.state)
    def scoped_pids(self):
        result=set()
        for p in self.cgroup.rglob('cgroup.procs'):
            try:result.update(int(value) for value in p.read_text().split())
            except OSError:pass
        return result-{os.getpid()}
    def stop_owned(self):
        # No global pkill; pidfds bind signals to the actual owned process instance.
        opened=[]
        for pid in self.scoped_pids():
            fd=None
            try:
                fd=os.pidfd_open(pid)
                if pid not in self.scoped_pids():continue
                bound=fd;opened.append(bound);fd=None
                try:args=proc_args(pid)
                except OSError:args=[]
                # Give rosbag a chance to finalize, others receive orderly termination.
                sig=signal.SIGINT if b'bag' in args and b'record' in args else signal.SIGTERM
                signal.pidfd_send_signal(bound,sig)
            except OSError:pass
            finally:
                if fd is not None:os.close(fd)
        deadline=time.monotonic()+10
        try:
            while self.scoped_pids() and time.monotonic()<deadline:
                self.publish();time.sleep(.1)
                if self.child:self.child.poll()
            for fd in opened:
                try:signal.pidfd_send_signal(fd,signal.SIGKILL)
                except ProcessLookupError:pass
        finally:
            for fd in opened:os.close(fd)
    def cancelled(self):return self.stopping or (self.folder/'stop.json').exists()
    def await_confirmation(self):
        self.state.update(phase='awaiting_confirmation',nonce=secrets.token_hex(24),confirmation_deadline=time.time()+120)
        self.publish()
        while not self.cancelled():
            if time.time()>self.state['confirmation_deadline']:raise ValueError('启动确认超时，已取消准备')
            path=self.folder/'confirm.json'
            if path.exists():
                value=json.loads(path.read_text());path.unlink()
                if not secrets.compare_digest(str(value.get('nonce','')),self.state['nonce']):raise ValueError('启动确认不匹配')
                self.state.update(nonce=None,phase='starting');self.publish();return True
            if self.child is not None and self.child.poll() is not None:raise ValueError('原遥操预检进程已退出')
            self.publish();time.sleep(.1)
        return False
    def launch_pty(self):
        if self.cancelled():raise ValueError('启动已取消')
        if hashlib.sha256(Path(self.cfg['start']).read_bytes()).hexdigest()!=self.cfg['start_sha256']:raise ValueError('确认期间启动脚本发生变化')
        self.master,slave=pty.openpty();os.set_blocking(self.master,False)
        self.child=subprocess.Popen(['bash',self.cfg['start']],cwd=Path(self.cfg['cwd']).resolve(),stdin=slave,stdout=slave,stderr=slave,start_new_session=True)
        os.close(slave)
    def drain(self):
        if self.master is None:return
        try:
            while True:
                data=os.read(self.master,8192)
                if not data:break
                text=self.decoder.decode(data);self.log.write(text);self.text=(self.text+text)[-24000:]
        except (BlockingIOError,OSError):pass
    def prepare_pcservice(self):
        if any('pcservice' in types for types in observed().values()):return
        self.log.write('启动本会话专属PCService；退出时仅清理本进程组。\n')
        subprocess.Popen(['bash','/opt/apps/roboticsservice/runService.sh'],cwd='/opt/apps/roboticsservice',stdout=self.log,stderr=subprocess.STDOUT)
        self.state['pcservice_owned']=True
        until=time.monotonic()+15
        while not self.cancelled() and time.monotonic()<until:
            try:
                for port in (60061,63901):
                    with socket.create_connection(('127.0.0.1',port),timeout=.2):pass
                return
            except OSError:self.publish();time.sleep(.2)
        raise ValueError('PCService端口未就绪或已取消')
    def run(self):
        profile=read_profile()
        if Path('/etc/machine-id').read_text().strip()!=profile['expected_machine_id']:raise ValueError('机器人身份不匹配')
        if hashlib.sha256(Path(self.cfg['start']).read_bytes()).hexdigest()!=self.cfg['start_sha256']:raise ValueError('启动脚本内容变化')
        check_hardware()
        existing=observed()
        if any(types&{'upper','lxl_bridge','lxl_teleop'} for types in existing.values()):raise ValueError('已有现场/其他遥操进程，先从原入口结束；网页不接管它们')
        if self.variant=='lxl':
            if any('pcservice' in types for types in existing.values()):raise ValueError('已有外部PCService；LXL原脚本会重复启动它，请先结束外部XR服务，或使用meaxiao版复用它')
            config=legacy_config()
            if config.get('local_ip')!=profile['ip']:raise ValueError('LXL local_ip与机器人局域网地址不符')
            for side in ('left','right'):
                values=config.get('target_'+side)
                if not isinstance(values,list) or len(values)!=7 or not all(type(v) in (int,float) and math.isfinite(v) for v in values):raise ValueError('不能核实LXL初始姿态配置')
            self.state['legacy_config']=config
            self.control_lock=Path(installation_environment()['F1_CONTROL_LOCK']).open('a+')
            fcntl.flock(self.control_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.control_lock.seek(0);self.control_lock.truncate();json.dump({'owner':'lxl_web','pid':os.getpid(),'process_start':start_ticks(os.getpid())},self.control_lock);self.control_lock.flush()
            if not self.await_confirmation():return
            check_hardware()
            self.launch_pty()
            self.state['pcservice_owned']=True
        else:
            self.prepare_pcservice()
            if self.cancelled():return
            self.launch_pty()
            until=time.monotonic()+90
            while not self.cancelled() and PROMPT not in self.text:
                self.drain();self.publish()
                if self.child.poll() is not None:raise ValueError('meaxiao原预检未通过，见会话日志')
                if time.monotonic()>until:raise ValueError('meaxiao预检超时')
                time.sleep(.1)
            if self.cancelled() or not self.await_confirmation():return
            check_hardware()
            # The browser operator explicitly confirms the original terminal prompt.
            if self.cancelled():return
            os.write(self.master,b'\n')
            self.state['phase']='waiting_xr'
        started=time.monotonic()
        while not self.cancelled():
            self.drain();owned={pid:kinds for pid,kinds in observed().items() if pid in self.scoped_pids()}
            kinds=set().union(*owned.values()) if owned else set()
            if self.variant=='lxl':
                ready={'lxl_bridge','lxl_teleop','pcservice'}.issubset(kinds)
                if self.child.poll() not in (None,0):raise ValueError('LXL启动脚本失败')
                if ready:self.state['phase']='running'
                elif self.state['phase']=='running' or time.monotonic()-started>35:raise ValueError('LXL进程不完整；成功打印不代表启动成功，检查原遥操日志')
            else:
                if self.child.poll() is not None:raise ValueError('meaxiao会话已退出，见日志')
                # Supervisor only prints STARTING after both command workers were spawned.
                if 'PICO_UPPER_STARTING' in self.text:
                    self.state['phase']='workers_starting'
                    if self.native_log.exists():
                        with self.native_log.open('rb') as stream:
                            stream.seek(max(self.native_log_start,self.native_log.stat().st_size-12000))
                            if b'UPPER_TELEOP_READY' in stream.read():self.state['phase']='running'
            self.state['owned_processes']=[{'pid':pid,'components':sorted(types)} for pid,types in owned.items()]
            self.publish();time.sleep(.15)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--session',required=True);args=parser.parse_args()
    folder=Path(args.session).resolve();base=(Path(installation_environment()['F1_CONSOLE_RUNTIME'])/'teleop/sessions').resolve()
    if folder.parent!=base or len(folder.name)!=32 or any(c not in '0123456789abcdef' for c in folder.name):raise ValueError('invalid fixed session directory')
    os.umask(0o077);session=Session(folder)
    def cancel(*_):session.stopping=True
    signal.signal(signal.SIGTERM,cancel);signal.signal(signal.SIGINT,cancel)
    failed=False
    try:session.publish();session.run()
    except Exception as exc:
        failed=True;session.state['error']=str(exc);session.log.write('SESSION_ERROR '+str(exc)+'\n')
    finally:
        session.state.update(phase='stopping',nonce=None);session.publish()
        session.stop_owned()
        if session.master is not None:os.close(session.master)
        if session.control_lock:
            session.control_lock.seek(0);session.control_lock.truncate();session.control_lock.close()
        session.state['phase']='failed' if failed else 'stopped';session.publish();session.log.close()
if __name__=='__main__':main()

"""HTTP gateway for fixed Pico launchers. GET never launches a process."""
import ast,hashlib,json,os,re,secrets,subprocess,threading,time
from pathlib import Path
from core import atomic_json
from recording_service import small_json
from project_paths import installation_environment, teleop_config

UNIT='f1-4242-teleop-session.service'
TERMINAL=('idle','stopped','failed')

def legacy_config(path=None):
    path=Path(path) if path is not None else Path(installation_environment()['F1_LXL_ROOT'])/'ip_config.py'
    values={}
    try:
        for node in ast.parse(Path(path).read_text()).body:
            if isinstance(node,ast.Assign):
                for target in node.targets:
                    if isinstance(target,ast.Name) and target.id in ('local_ip','target_left','target_right','reset_left','reset_right'):
                        try:values[target.id]=ast.literal_eval(node.value)
                        except (ValueError,TypeError):pass
    except (OSError,SyntaxError):pass
    return values

class TeleopService:
    def __init__(self,runtime,root,offline=False):
        self.root=Path(root).resolve();self.runtime=Path(runtime).resolve();self.owned=self.runtime/'teleop';self.offline=offline;self.lock=threading.Lock()
        self.config=teleop_config(self.root/'teleop/config.json', self.runtime)
    def current(self):
        value=small_json(self.owned/'current.json',{})
        sid=value.get('id','')
        if not re.fullmatch('[0-9a-f]{32}',sid):return None
        folder=self.owned/'sessions'/sid
        if folder.resolve()!=folder:raise ValueError('invalid teleop session path')
        return folder
    def state(self,owner=None):
        state={'phase':'idle','variant':None,'managed':True,'can_confirm':False,'can_stop':False,'available':not self.offline,'legacy_config':legacy_config() if not self.offline else {}}
        folder=self.current()
        if folder is None:return state
        request=small_json(folder/'request.json',{})
        data=small_json(folder/'status.json',{'phase':'starting','checked_unix':request.get('created_unix',0),'variant':request.get('variant')})
        state.update(data);state['id']=folder.name
        state['status_age_s']=time.time()-float(data.get('checked_unix',0))
        state['status_ttl_s']=12 if not data.get('pid') else 4
        alive=False
        if state['phase'] not in TERMINAL:
            age=time.time()-float(data.get('checked_unix',0))
            limit=12 if not data.get('pid') else 4
            alive=True
            if data.get('pid'):
                try:
                    proc=Path('/proc')/str(data['pid']);args=(proc/'cmdline').read_bytes().split(b'\0')
                    alive=b'teleop.worker' in args and str(folder).encode() in args and (proc/'stat').read_text().rsplit(')',1)[1].split()[19]==data.get('process_start')
                except OSError:alive=False
            if not 0<=age<=limit or not alive:state.update(phase='failed',error='遥操会话进程或状态已失联；请查看会话日志，未自动重启遥操')
        state['can_stop']=bool(alive and state.get('pid'))
        state['can_confirm']=bool(not (folder/'confirm.json').exists() and not (folder/'stop.json').exists() and owner and request.get('owner_hash')==hashlib.sha256(owner.encode()).hexdigest() and state['phase']=='awaiting_confirmation')
        if not state['can_confirm']:state.pop('nonce',None)
        log=folder/'session.log'
        if log.is_file():
            with log.open('rb') as stream:stream.seek(max(0,log.stat().st_size-6000));state['log_tail']=stream.read().decode(errors='replace')
        return state
    def prepare(self,variant,owner):
        if self.offline:raise ValueError('离线预览不启动遥操')
        if not isinstance(variant,str) or variant not in self.config:raise ValueError('未知遥操版本')
        with self.lock:
            if self.state()['phase'] not in TERMINAL:raise ValueError('请先结束当前遥操会话')
            cfg=self.config[variant];source=Path(cfg['start'])
            if hashlib.sha256(source.read_bytes()).hexdigest()!=cfg['start_sha256']:raise ValueError('启动脚本已变化，需要重新核对；未执行')
            sid=secrets.token_hex(16);folder=self.owned/'sessions'/sid;folder.mkdir(parents=True,mode=0o700)
            request={'id':sid,'variant':variant,'owner_hash':hashlib.sha256(owner.encode()).hexdigest(),'created_unix':time.time(),'direct_start':True}
            atomic_json(folder/'request.json',request)
            args=['systemd-run','--user','--collect','--unit='+UNIT,'--property=Restart=no','--property=KillMode=control-group','--property=TimeoutStopSec=18','--property=WorkingDirectory='+str(self.root), '--setenv=PYTHONDONTWRITEBYTECODE=1','--setenv=PYTHONUNBUFFERED=1', '/usr/bin/python3','-m','teleop.worker','--session',str(folder)]
            env_args=['--setenv='+k+'='+v for k,v in installation_environment(self.runtime).items()]
            args[args.index('/usr/bin/python3'):args.index('/usr/bin/python3')]=env_args
            result=subprocess.run(args,capture_output=True,text=True,timeout=8)
            if result.returncode:raise ValueError('会话未启动：'+result.stderr[-1200:])
            atomic_json(self.owned/'current.json',{'id':sid})
            return self.state(owner)
    def command(self,operation,owner,session_id,nonce=None):
        with self.lock:
            state=self.state(owner);folder=self.current()
            if folder is None or session_id!=folder.name:raise ValueError('会话已变化，请刷新后操作')
            if operation=='confirm':
                if not state.get('can_confirm') or not isinstance(nonce,str) or not secrets.compare_digest(nonce,state.get('nonce','')):raise ValueError('启动确认已失效，或不属于当前浏览器')
                atomic_json(folder/'confirm.json',{'nonce':nonce,'confirmed_unix':time.time()})
            elif operation=='stop':
                if state['phase'] in TERMINAL and not state.get('can_stop'):raise ValueError('没有由网页管理的活动会话；现场启动的程序需在原入口停止')
                atomic_json(folder/'stop.json',{'requested_unix':time.time()})
            else:raise ValueError('未知遥操操作')
            return self.state(owner)

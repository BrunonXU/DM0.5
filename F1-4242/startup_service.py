"""Explicit, fixed-path base startup; never automatic and never a shell API."""
from pathlib import Path
from project_paths import expand_path
import hashlib,subprocess,threading,time

SCRIPT=Path('/home/user/workspace/start_all.sh')
UNIT='f1-4242-base-start.service'
MARKERS=('motor_ctl_node','kinematics_node','head_gemini330.launch.py','rs_launch_right_wrist_d405.py','rs_launch_left_wrist_d405.py','waist_updown_ros2.py','whole_body_ctl.py','gamepad_ctl.py')
class StartupService:
    def __init__(self,profile,runtime,offline=False,run=subprocess.run,processes=None):
        self.script=Path(expand_path(profile["startup_script"])) if profile.get("startup_script") else SCRIPT
        self.profile=profile;self.runtime=Path(runtime);self.offline=offline;self.run=run;self.processes=processes or self.read_processes;self.lock=threading.Lock();self.cache=None;self.at=0
    @staticmethod
    def read_processes():
        rows=[]
        for p in Path('/proc').iterdir():
            if not p.name.isdigit():continue
            try:args=(p/'cmdline').read_bytes().split(b'\0')
            except OSError:continue
            # Never match script text inside a diagnostic shell/python argument.
            tokens=[x.decode(errors='replace') for x in args if b'\n' not in x and len(x)<500]
            if any(any(token.endswith(m) for m in MARKERS) for token in tokens):rows.append(int(p.name))
        return rows
    def state(self,force=False):
        if not force and self.cache is not None and time.monotonic()-self.at<3:return dict(self.cache)
        processes=self.processes();exists=self.script.is_file();valid=exists and hashlib.sha256(self.script.read_bytes()).hexdigest()==self.profile.get('startup_script_sha256')
        phase='offline' if self.offline else 'unknown'
        detail={}
        if not self.offline:
            result=self.run(['systemctl','--user','show',UNIT,'-p','ActiveState','-p','SubState','-p','Result','-p','ExecMainStatus'],capture_output=True,text=True,timeout=3)
            detail=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
            phase=detail.get('ActiveState') or result.stdout.strip() or 'unknown'
        reason='离线开发环境' if self.offline else '原脚本缺失或内容已变化，需要重新核对' if not valid else '基础节点已存在，请勿重复启动；缺少的节点需单独排查' if processes else '启动任务已运行' if phase in ('active','activating','deactivating') else None
        stage='offline' if self.offline else 'starting' if phase in ('activating','deactivating') else 'failed' if phase=='failed' or detail.get('Result') not in (None,'','success') else 'completed' if phase=='active' and detail.get('SubState')=='exited' else 'running' if processes else 'idle'
        message={'offline':'离线开发环境','starting':'基础启动中','completed':'启动脚本已完成；设备就绪以实际反馈为准','failed':'启动脚本失败，请查看日志','running':'检测到基础节点','idle':'基础节点未启动'}[stage]
        value={'stage':stage,'message':message,'result':detail.get('Result'),'exit_status':detail.get('ExecMainStatus'),'can_start':reason is None,'reason':reason,'phase':phase,'existing_pids':processes,'script':str(self.script),'ec_script':self.profile.get('ec_startup_script','/home/user/workspace/start_ec_master.sh'),'script_verified':valid,'scope':'驱动、运动学、三相机、原升降/全身/手柄节点；不启动 EC-Master、Pico 或录制'}
        self.cache=value;self.at=time.monotonic();return dict(value)
    def start(self):
        with self.lock:
            state=self.state(force=True)
            if not state['can_start']:raise ValueError(state['reason'])
            log=self.runtime/'base-start.log'
            command=['systemd-run','--user','--no-block','--unit='+UNIT,'--property=Type=oneshot','--property=RemainAfterExit=yes','--property=TimeoutStartSec=90','--property=StandardOutput=append:'+str(log),'--property=StandardError=append:'+str(log),'--working-directory='+str(self.script.parent),'/bin/bash',str(self.script)]
            try:
                r=self.run(command,capture_output=True,text=True,timeout=5)
            except subprocess.TimeoutExpired:
                self.cache=None
                return {'accepted':None,'unit':UNIT,'log':str(log),'message':'受理结果待核对，请刷新状态；不要重复启动'}
            if r.returncode:raise ValueError('基础启动任务未受理：'+r.stderr.strip()[:500])
            self.cache=None
            return {'accepted':True,'unit':UNIT,'log':str(log),'message':'启动请求已受理，设备就绪以关节和相机反馈为准'}

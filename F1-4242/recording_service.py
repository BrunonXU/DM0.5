"""HTTP-side lifecycle of our isolated LeRobot worker. No ROS or motion imports."""
from __future__ import annotations
import hashlib,json,math,os,socket,stat,subprocess,threading,time
from pathlib import Path
from project_paths import installation_environment

CAMERA_FILES={'head':'observation.images.base_0_rgb.mp4','left':'observation.images.left_wrist_0_rgb.mp4','right':'observation.images.right_wrist_0_rgb.mp4'}

def confined_file(root,relative):
    original=Path(root).absolute()
    if any(p.is_symlink() for p in (original,*original.parents)):raise ValueError('资源根路径不能包含符号链接')
    root=original.resolve(); path=root/relative
    if not isinstance(relative,str) or Path(relative).is_absolute() or '..' in Path(relative).parts:raise ValueError('资源路径无效')
    current=root
    for part in Path(relative).parts:
        current=current/part
        if current.is_symlink():raise ValueError('资源不能为符号链接')
    if not path.resolve().is_relative_to(root) or not path.is_file():raise ValueError('资源不存在')
    return path

def small_json(path,default=None):
    path=Path(path)
    if not path.exists():return default
    if path.is_symlink() or path.stat().st_size>12_000_000:raise ValueError('状态文件大小或类型无效')
    value=json.loads(path.read_text())
    if not isinstance(value,dict):raise ValueError('状态文件需要JSON对象')
    return value

class RecordingService:
    def __init__(self,runtime,code_root,offline=False):
        self.runtime=Path(runtime).resolve();self.code_root=Path(code_root).resolve();self.owned=self.runtime/'recorder'
        self.output=self.runtime/'recordings/upper16_v1';self.offline=offline;self.lock=threading.Lock();self.child=None
    def _alive(self,state):
        try:
            pid=state['pid']
            if not isinstance(pid,int) or pid<=0:return False
            args=(Path('/proc')/str(pid)/'cmdline').read_bytes().split(b'\0')
            return b'recording.worker' in args and str(self.runtime).encode() in args
        except (OSError,KeyError,TypeError):return False
    def state(self):
        output = self.output
        config = small_json(self.runtime/'recording.json', {})
        if config.get('recording_format') == 'f1_capture_v2':
            from recording.unified.storage import read_config
            output = Path(read_config(self.runtime/'storage.json')['paths']['pico'])
        base={'mode':'not_started','available':not self.offline,'ready':False,'ready_reasons':[],
              'output_root':str(output),'contract':'f1_upper_16d_v1','task':'F1 upper-body bimanual teleoperation',
              'frames':0,'elapsed_s':0,'last_saved':None,'review':None}
        if self.offline:return dict(base,mode='unavailable',ready_reasons=['离线预览不启动录制'])
        python=Path(os.environ.get('F1_RECORDING_PYTHON',str(self.runtime/'recording-venv/bin/python')))
        if not python.exists():return dict(base,mode='unavailable',available=False,ready_reasons=['独立录制环境尚未安装'])
        starting=self.child is not None and self.child.poll() is None
        try:
            data=small_json(self.owned/'status.json',{})
            if not data:return dict(base,mode='starting') if starting else base
            if not self._alive(data) or not (self.owned/'control.sock').exists():
                if starting:return dict(base,mode='starting')
                return dict(base,last_error='上次录制服务已停止；未提交数据保留在独立目录',last_saved=data.get('last_saved'))
            age=time.time()-float(data.get('checked_unix',0))
            if not math.isfinite(age) or age<0 or age>4:return dict(base,mode='error',last_error='录制服务状态过期',ready_reasons=['录制状态未及时更新，请查看独立录制日志'])
            return dict(data,status_age_s=age)
        except (ValueError,OSError,TypeError) as exc:return dict(base,mode='error',last_error=str(exc))
    def ensure_worker(self):
        if self.offline:raise ValueError('离线预览不能启动录制')
        state=self.state()
        if state['mode'] not in ('not_started','starting','unavailable'):
            if state['mode']=='error':raise ValueError(state.get('last_error','录制器状态异常'))
            return state
        if not Path(os.environ.get('F1_RECORDING_PYTHON',str(self.runtime/'recording-venv/bin/python'))).exists():raise ValueError('独立录制环境尚未安装')
        self.owned.mkdir(parents=True,exist_ok=True)
        if self.owned.is_symlink():raise ValueError('录制运行目录不能为符号链接')
        if self.child is None or self.child.poll() is not None:
            with (self.owned/'worker.log').open('ab') as log:
                self.child=subprocess.Popen(['bash',str(self.code_root/'recording/run.sh')],env=dict(os.environ,**installation_environment(self.runtime)),stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        end=time.monotonic()+20
        while time.monotonic()<end:
            if self.child.poll() is not None:raise ValueError('独立录制服务启动失败，请查看 recorder/worker.log')
            state=self.state()
            if state['mode'] not in ('not_started','unavailable','starting'):return state
            time.sleep(.1)
        raise ValueError('录制服务仍在启动，请稍后刷新状态；尚未开始采集')
    def command(self,operation):
        if operation not in ('start','stop','save','discard'):raise ValueError('未知录制命令')
        with self.lock:
            if operation=='start':
                was_stopped=self.state()['mode'] in ('not_started','starting')
                state=self.ensure_worker()
                # First click only prepares subscriptions; real start is a second explicit click.
                if was_stopped:return state
            state=self.state()
            if state['mode'] in ('not_started','unavailable','error'):raise ValueError(state.get('last_error') or '录制服务未就绪')
            sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);sock.settimeout(110)
            try:
                sock.connect(str(self.owned/'control.sock'));sock.sendall(json.dumps({'operation':operation}).encode()+b'\n')
                with sock.makefile('rb') as stream:raw=stream.readline(1_000_001)
                if len(raw)>1_000_000:raise ValueError('录制服务响应过大')
                result=json.loads(raw)
                if not result.get('ok'):raise ValueError(result.get('error','录制操作未完成'))
                return result['recording']
            except (OSError,ValueError) as exc:raise ValueError(str(exc)) from exc
            finally:sock.close()
    def catalog(self):
        try:data=small_json(self.owned/'catalog.json',{})
        except (ValueError,OSError):return []
        rows=[]
        for original in data.get('episodes',[]):
            try:
                row=dict(original);eid=row['id'];signature=[]
                row['path']=str(self.output/row['relative_path'])
                if len(eid)!=24 or any(c not in '0123456789abcdef' for c in eid):continue
                for item in row['files']:
                    p=confined_file(self.output,item['relative_path']);st=p.stat();signature.append([item['relative_path'],st.st_size,st.st_mtime_ns])
                row['source_signature']=hashlib.sha256(json.dumps([str(self.output),eid,sorted(signature),row['metadata_sha256']],sort_keys=True).encode()).hexdigest()
                analytics=self.owned/'previews'/(eid+'.json')
                if analytics.is_file():row['analytics_url']='/api/data/analytics/'+eid
                rows.append(row)
            except (ValueError,OSError,KeyError,TypeError):continue
        return rows
    def episode(self,eid):
        for row in self.catalog():
            if row['id']==eid:return row
        raise ValueError('LeRobot 数据不存在')
    def video(self,eid,camera):
        if camera not in CAMERA_FILES:raise ValueError('相机无效')
        row=self.episode(eid)
        for item in row.get('videos',[]):
            if item['camera']==camera:return confined_file(self.output,item['relative_path'])
        raise ValueError('视频不存在')
    def review_video(self,camera):
        if camera not in CAMERA_FILES:raise ValueError('相机无效')
        state=self.state();review=state.get('review')
        if state['mode']!='review' or not review:raise ValueError('当前没有待审核视频')
        return confined_file(self.output,str(Path(review['relative_path'])/CAMERA_FILES[camera]))
    def analytics(self,eid):
        self.episode(eid)
        return confined_file(self.owned/'previews',eid+'.json')

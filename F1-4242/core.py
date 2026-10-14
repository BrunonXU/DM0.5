"""Framework-independent console services. No ROS imports or automatic motion."""
from __future__ import annotations
import collections,copy,fcntl,hashlib,json,math,os,secrets,threading,time,uuid
from datetime import datetime,timezone
from pathlib import Path

def utc(): return datetime.now(timezone.utc).isoformat()
def finite(x): return not isinstance(x,bool) and isinstance(x,(int,float)) and math.isfinite(x)
def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    with tmp.open('x') as f:
        json.dump(value,f,ensure_ascii=False,allow_nan=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)

class EventLog:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.rows=collections.deque(maxlen=300);self.lock=threading.RLock()
    def add(self,kind,message,**data):
        row=dict(id=uuid.uuid4().hex,at=utc(),kind=kind,message=message,**data)
        with self.lock:
            self.rows.append(row)
            with self.path.open('a') as f:f.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
        return row
    def list(self):
        with self.lock:return list(reversed(copy.deepcopy(self.rows)))

class PresetStore:
    def __init__(self,path,profile):
        self.path=Path(path);self.profile=profile;self.lock=threading.RLock()
        self.seed_defaults()
    def seed_defaults(self):
        defaults=self.profile.get('default_presets',[])
        if not defaults:return
        marker=self.path.with_name(self.path.stem+'.defaults.json')
        seen=json.loads(marker.read_text()) if marker.exists() else []
        with self.lock:
            rows=self.all()
            for preset in defaults:
                key=preset['key']
                if key in seen:continue
                row=self.validate(preset);row['id']='default-'+key
                if not any(item['id']==row['id'] for item in rows):rows.insert(0,row)
                seen.append(key)
            atomic_json(self.path,rows)
            atomic_json(marker,seen)
    def all(self):
        with self.lock:
            return json.loads(self.path.read_text()) if self.path.exists() else []
    def validate(self,p):
        if not isinstance(p,dict):raise ValueError('预设必须是对象')
        if p.get('units','degree')!='degree':raise ValueError('预设单位必须是 degree（角度）')
        if p.get('robot_id',self.profile['id'])!=self.profile['id']:raise ValueError('预设属于另一台机器人')
        name=p.get('name','').strip()
        if not name or len(name)>80:raise ValueError('请输入1–80字符的预设名称')
        values=p.get('joints')
        if not isinstance(values,dict) or not values:raise ValueError('预设需要具名关节目标')
        for key,value in values.items():
            bounds=self.profile['limits'].get(key)
            if not bounds or not finite(value) or not bounds[0]<=value<=bounds[1]:raise ValueError('关节目标无效或超出配置范围: '+str(key))
        grip=p.get('grippers',{})
        if not isinstance(grip,dict):raise ValueError('夹爪配置必须为对象')
        if any(side not in ('left','right') or not finite(v) or not 0<=v<=100 for side,v in grip.items()):raise ValueError('夹爪目标无效')
        return dict(id=uuid.uuid4().hex,name=name,robot_id=self.profile['id'],schema='f1_console_preset_v1',units='degree',joints=dict(values),grippers=grip,created_at=utc(),source=p.get('source','edited'),profile_sha256=self.profile.get('profile_sha256'))
    def save(self,p):
        row=self.validate(p)
        with self.lock:
            rows=self.all();rows.append(row);atomic_json(self.path,rows)
        return row
    def import_many(self,rows):
        if not isinstance(rows,list) or not 0<len(rows)<=100:raise ValueError('一次导入1–100条预设')
        checked=[self.validate(p) for p in rows]
        with self.lock:atomic_json(self.path,self.all()+checked)
        return checked
    def delete(self,pid):
        with self.lock:
            rows=self.all();kept=[r for r in rows if r['id']!=pid]
            if len(kept)==len(rows):raise ValueError('预设不存在')
            atomic_json(self.path,kept)

class ControlLease:
    """Cross-process exclusion shared with Pico. Lock held through cancellation."""
    def __init__(self,path,ttl=3.,clock=time.monotonic):
        self.path=Path(path);self.ttl=ttl;self.clock=clock;self.lock=threading.RLock()
        self.fd=None;self.owner=None;self.token=None;self.deadline=0;self.stopping=False
    def acquire(self,session):
        with self.lock:
            if self.fd is not None:
                if self.owner==session and self.deadline>self.clock() and not self.stopping:
                    self.deadline=self.clock()+self.ttl;return self.token
                raise ValueError('控制权正被其他会话占用或等待停止')
            self.path.parent.mkdir(parents=True,exist_ok=True)
            fd=self.path.open('a+')
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                fd.close();raise ValueError('Pico或其他研发控制程序正在占用机器人')
            fd.seek(0);fd.truncate();json.dump({'owner':'console','pid':os.getpid()},fd);fd.flush()
            self.fd=fd;self.owner=session;self.token=secrets.token_urlsafe(24);self.deadline=self.clock()+self.ttl;self.stopping=False
            return self.token
    def require(self,session,token,renew=False):
        with self.lock:
            if self.fd is None or self.owner!=session or not isinstance(token,str) or not secrets.compare_digest(token,self.token) or self.deadline<=self.clock() or self.stopping:
                raise ValueError('控制权已失效，请重新接管')
            if renew:self.deadline=self.clock()+self.ttl
    def snapshot(self,session):
        with self.lock:
            external=None
            if self.fd is None and self.path.exists():
                with self.path.open('r') as probe:
                    try:
                        fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        fcntl.flock(probe,fcntl.LOCK_UN)
                    except BlockingIOError:
                        try:external=json.load(probe).get('owner') or '其他研发程序'
                        except (ValueError,AttributeError):external='其他研发程序'
            return dict(owner=(('当前浏览器' if self.owner==session else '其他浏览器') if self.fd else external),lease_id=self.token if self.owner==session and not self.stopping else None,expires_in_s=max(0,self.deadline-self.clock()) if self.fd else 0,available=self.fd is None and external is None,stopping=self.stopping,reason='共享控制锁已占用' if external else None)
    def release(self):
        with self.lock:
            if self.fd:
                fcntl.flock(self.fd,fcntl.LOCK_UN);self.fd.close()
            self.fd=None;self.owner=None;self.token=None;self.deadline=0;self.stopping=False

class ActionManager:
    def __init__(self,adapter,events):
        self.adapter=adapter;self.events=events;self.lock=threading.RLock();self.rows={};self.active=None;self.cancel_event=None;self.thread=None
    def latest(self):
        with self.lock:
            return copy.deepcopy(self.rows.get(self.active)) if self.active else None
    def busy(self):
        with self.lock:return self.thread is not None and self.thread.is_alive()
    def start(self,spec,session):
        # The HTTP layer must validate lease while holding its operation mutex.
        with self.lock:
            if self.busy():
                row=self.rows[self.active]
                if spec['kind']=='base' and row['kind']=='base' and row['session']==session:
                    if self.adapter.renew_base(spec):return copy.deepcopy(row)
                raise ValueError('已有动作正在执行或停止')
            aid=uuid.uuid4().hex
            row=dict(id=aid,kind=spec['kind'],status='accepted',progress=0,created_at=utc(),session=session,targets=spec.get('targets',{}))
            if spec.get('replay'):row['replay']=copy.deepcopy(spec['replay'])
            self.rows[aid]=row;self.active=aid;self.cancel_event=threading.Event()
            cancel=self.cancel_event
            def progress(value):
                with self.lock:
                    if isinstance(value,dict):row['progress_detail']=value;row['progress']=value.get('progress',row['progress'])
                    elif finite(value):row['progress']=value
            def work():
                try:
                    with self.lock:row['status']='running'
                    result=self.adapter.execute(spec,cancel,progress)
                    with self.lock:
                        row['result']=result;row['status']='cancelled' if cancel.is_set() else result.get('status','failed');row['progress']=1 if row['status']=='succeeded' else row['progress']
                except Exception as exc:
                    with self.lock:
                        sent=bool(getattr(exc,'commands_sent',False))
                        row['status']='cancelled' if cancel.is_set() else ('failed' if sent else 'rejected');row['error']=str(exc);row['result']={'commands_sent':sent,'feedback_verified':False}
                        if hasattr(exc,'command_timing'):row['result']['command_timing']=exc.command_timing
                        if hasattr(exc,'parts'):row['result']['parts']=exc.parts
                        if hasattr(exc,'recovery'):row['result']['head_boundary_recovery']=exc.recovery
                        for field in ('hold_sent','physical_stop_confirmed'):
                            if hasattr(exc,field):row['result'][field]=getattr(exc,field)
                finally:
                    with self.lock:row['finished_at']=utc()
                    self.events.add('action_result',row['status'],action_id=aid,kind_name=spec['kind'],error=row.get('error'),result=row.get('result'))
            self.events.add('action_request','收到动作请求',action_id=aid,action_kind=spec['kind'],targets=spec.get('targets',{}),replay=spec.get('replay'))
            self.thread=threading.Thread(target=work,name='console-action',daemon=True);self.thread.start()
            # Bounded runtime history; leave current record untouched.
            while len(self.rows)>200:self.rows.pop(next(iter(self.rows)))
            return copy.deepcopy(row)
    def stop(self,wait=0.):
        with self.lock:
            thread=self.thread
            if thread and thread.is_alive():
                row=self.rows[self.active]
                # Event logging may outlive terminal result publication. A late
                # stop must not turn that finished action back into 'stopping'.
                if row['status'] in ('accepted','queued','running','stopping'):
                    self.cancel_event.set();row['status']='stopping'
        if thread and wait:thread.join(wait)
        return not self.busy()
    def get(self,aid):
        with self.lock:
            if aid not in self.rows:raise ValueError('动作不存在')
            return copy.deepcopy(self.rows[aid])

class OfflineAdapter:
    def __init__(self,error='未加载机器人ROS环境'):self.error=error
    def start(self):pass
    def close(self):pass
    def snapshot(self):return dict(connected=False,joints={},grippers={},base={},cameras={},battery=None,graph={},error=self.error)
    def capabilities(self):return {'motion_enabled':False,'reason':self.error}
    def camera(self,name):return None
    def execute(self,*a,**k):raise ValueError(self.error)
    def renew_base(self,*a,**k):return False

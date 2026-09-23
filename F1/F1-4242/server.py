#!/usr/bin/env python3
"""F1 local workbench: static hot reload, state, presets and explicit actions."""
from __future__ import annotations
import argparse,copy,hashlib,http.cookies,json,mimetypes,os,secrets,signal,sys,threading,time,urllib.parse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from core import ActionManager,ControlLease,EventLog,OfflineAdapter,PresetStore,finite,utc

ROOT=Path(__file__).resolve().parent
class App:
    def __init__(self,profile,runtime,control_lock,offline=False,web_root=None):
        self.web_root=Path(web_root or ROOT/'web').resolve()
        self.profile=profile;self.runtime=Path(runtime);self.runtime.mkdir(parents=True,exist_ok=True)
        self.events=EventLog(self.runtime/'events.jsonl');self.presets=PresetStore(self.runtime/'presets.json',profile)
        self.health_cache={};self.health_at=0
        self.sessions={};self.session_lock=threading.RLock();self.op_lock=threading.RLock();self.closed=threading.Event()
        self.token_path=self.runtime/'access.token'
        if not self.token_path.exists():
            fd=os.open(self.token_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as f:f.write(secrets.token_urlsafe(24)+'\n')
        self.token=self.token_path.read_text().strip()
        if not self.token:raise ValueError('Empty access token')
        self.contract={'ok':False,'scope':'offline_preview'}
        self.adapter=OfflineAdapter('本地开发预览，未连接ROS')
        if not offline:
            try:
                from robot.adapter import RosAdapter
                from robot.runtime_contract import verify_runtime_contract
                self.contract=verify_runtime_contract(profile)
                if not self.contract['ok']:
                    profile['motion_enabled']=False
                    self.events.add('contract_failed','动作保持关闭',errors=self.contract['errors'])
                self.adapter=RosAdapter(profile);self.adapter.start()
            except Exception as exc:
                self.events.add('adapter_error','ROS订阅未启动',error=str(exc));self.adapter=OfflineAdapter(str(exc))
        self.lease=ControlLease(control_lock);self.actions=ActionManager(self.adapter,self.events)
        self.watcher=threading.Thread(target=self._watch,name='lease-watch',daemon=True);self.watcher.start()
        self.events.add('server_started','工作台已启动；未触发动作或采集',robot=profile['id'])
    def _watch(self):
        while not self.closed.wait(.1):
            with self.op_lock:
                if self.lease.fd and (self.lease.deadline<=time.monotonic() or self.lease.stopping):
                    self.lease.stopping=True
                    if self.actions.stop():
                        self.lease.release();self.events.add('control_released','控制权到期或释放，动作已停止')
    def session(self,raw):
        cookies=http.cookies.SimpleCookie()
        try:cookies.load(raw or '')
        except http.cookies.CookieError:pass
        key=cookies.get('f1_console_session');sid=key.value if key else ''
        with self.session_lock:
            if sid not in self.sessions:
                sid=secrets.token_urlsafe(24);self.sessions[sid]={'authenticated':False,'created':time.monotonic()}
            return sid,self.sessions[sid]
    def state(self,sid):
        s=self.adapter.snapshot();s.update(robot_id=self.profile['id'],control=self.lease.snapshot(sid),action=self.actions.latest(),authenticated=self.sessions[sid]['authenticated'],capabilities=self.adapter.capabilities())
        s['system_health']=self.health();s['services']=s['system_health'].get('services',{})
        s['pico']={'mode':'upper_body_only','start_script':'/home/user/Bruno/F1/pico_upper/current/start.sh','status':'运行中' if s['services'].get('pico_upper',{}).get('running') else '未运行','version':'meaxiao 1.4.4 / upper-v2','commands':{k:'/home/user/Bruno/F1/pico_upper/current/'+k+'.sh' for k in ('check','start','stop')},'reason':'PCService已运行' if s['services'].get('pico_pc_service',{}).get('running') else 'PCService未运行；现场单独启动后再试遥操'}
        s['data']={'local_path':self.profile.get('data_path'),'mode':'unchanged','recording_control_enabled':False,'transfer_configured':False,'message':'现有数采逻辑未修改；服务器与分析平台位置待确定'}
        return s
    def health(self):
        if time.monotonic()-self.health_at>5:
            try:
                from health import snapshot
                self.health_cache=snapshot(self.profile.get("data_path","/home/user/lxl/pico_data"))
            except Exception as exc:self.health_cache={"error":str(exc)}
            self.health_at=time.monotonic()
        return self.health_cache
    def close(self):
        self.closed.set();self.actions.stop(wait=3)
        if not self.actions.busy():self.lease.release()
        self.adapter.close()
    def revision(self):
        h=hashlib.sha256()
        for p in sorted(self.web_root.rglob('*')):
            if p.is_file():h.update(str(p.relative_to(self.web_root)).encode());h.update(p.read_bytes())
        return h.hexdigest()[:16]

class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,app,allowed):
        self.app=app;self.allowed=set(allowed);super().__init__(address,Handler)

class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args):pass
    def send(self,status,data,content_type='application/json; charset=utf-8'):
        b=(json.dumps(data,ensure_ascii=False,allow_nan=False).encode() if content_type.startswith('application/json') else data)
        self.send_response(status);self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(b)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY');self.send_header('Referrer-Policy','same-origin')
        if getattr(self,'sid',None):self.send_header('Set-Cookie',f'f1_console_session={self.sid}; HttpOnly; SameSite=Strict; Path=/')
        self.end_headers()
        try:self.wfile.write(b)
        except (BrokenPipeError,ConnectionResetError):pass
    def prepare(self):
        host=self.headers.get('Host','')
        if host not in self.server.allowed:self.send(403,{'ok':False,'error':'未允许的Host'});return False
        self.sid,self.session_data=self.server.app.session(self.headers.get('Cookie'))
        return True
    def do_GET(self):
        if not self.prepare():return
        app=self.server.app;path=urllib.parse.urlsplit(self.path).path
        try:
            if path=='/api/session':data={'authenticated':self.session_data['authenticated']}
            elif path=='/api/robot':data={'robot':dict(app.profile,capabilities=app.adapter.capabilities())}
            elif path=='/api/state':data=app.state(self.sid)
            elif path=='/api/presets':data={'presets':app.presets.all()}
            elif path=='/api/events':data={'events':app.events.list()}
            elif path=='/api/dev/revision':data={'revision':app.revision()}
            elif path=='/api/storage':data={'health':app.health()}
            elif path=='/api/system':data={'robot':app.profile['id'],'version':app.profile['version'],'runtime':str(app.runtime),'data_mode':'unchanged','upstream_ui':'http://192.168.31.58/','motion_capabilities':app.adapter.capabilities(),'access_script':'./access.sh','hot_reload':True,'contract':app.contract,'ros_distro':'Jazzy','mode':'以本机动作能力检查为准'}
            elif path.startswith('/api/actions/'):data={'action':app.actions.get(path.rsplit('/',1)[-1])}
            elif path.startswith('/api/cameras/') and path.endswith('.jpg'):
                name=path.rsplit('/',1)[-1][:-4];frame=app.adapter.camera(name)
                if not frame or frame[2]>2:self.send(503,{'ok':False,'error':'相机没有新鲜图像'});return
                self.send(200,frame[0],frame[1]);return
            elif path.startswith('/api/'):self.send(404,{'ok':False,'error':'接口不存在'});return
            else:
                target=(app.web_root/('index.html' if path=='/' else urllib.parse.unquote(path).lstrip('/'))).resolve()
                if not target.is_relative_to(app.web_root) or not target.is_file():self.send(404,{'ok':False,'error':'文件不存在'});return
                self.send(200,target.read_bytes(),mimetypes.guess_type(str(target))[0] or 'application/octet-stream');return
            self.send(200,dict(ok=True,**data))
        except ValueError as exc:self.send(400,{'ok':False,'error':str(exc)})
        except Exception as exc:self.send(500,{'ok':False,'error':str(exc)})
    def do_POST(self):
        if not self.prepare():return
        host=self.headers.get('Host','');origin=self.headers.get('Origin','')
        if origin not in ('http://'+host,'https://'+host) or self.headers.get('X-F1-Request')!='1':self.send(403,{'ok':False,'error':'写操作需要同源工作台请求'});return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=256000:raise ValueError('请求大小无效')
            body=json.loads(self.rfile.read(length),parse_constant=lambda _: (_ for _ in ()).throw(ValueError('不允许非有限数值')))
            if not isinstance(body,dict):raise ValueError('请求需要JSON对象')
        except (ValueError,json.JSONDecodeError) as exc:self.send(400,{'ok':False,'error':str(exc)});return
        app=self.server.app;path=urllib.parse.urlsplit(self.path).path
        if path=='/api/login':
            if not isinstance(body.get('token'),str) or not secrets.compare_digest(body['token'],app.token):self.send(401,{'ok':False,'error':'访问码不正确'});return
            self.session_data['authenticated']=True;self.send(200,{'ok':True,'authenticated':True});return
        if not self.session_data['authenticated']:self.send(401,{'ok':False,'error':'请先启用操作并输入本机访问码'});return
        try:
            with app.op_lock:
                if path=='/api/logout':
                    if app.lease.owner==self.sid:app.lease.stopping=True;app.actions.stop()
                    self.session_data['authenticated']=False;data={'authenticated':False}
                elif path=='/api/control/acquire':
                    token=app.lease.acquire(self.sid);data={'lease_id':token,'expires_in_s':app.lease.ttl};app.events.add('control_acquired','浏览器取得研发控制权')
                elif path in ('/api/control/heartbeat','/api/control/release','/api/stop'):
                    app.lease.require(self.sid,body.get('lease_id'),renew=path.endswith('heartbeat'))
                    if path.endswith('heartbeat'):data={'expires_in_s':app.lease.ttl}
                    else:
                        done=app.actions.stop()
                        if path.endswith('release'):
                            app.lease.stopping=True
                            if done:app.lease.release()
                        data={'stopped':done,'action':app.actions.latest()}
                elif path=='/api/actions':
                    app.lease.require(self.sid,body.get('lease_id'),renew=True)
                    spec={k:v for k,v in body.items() if k not in ('lease_id',)}
                    if spec.get('kind')=='base_jog':spec={'kind':'base',**spec.get('targets',{}),'ttl_s':spec.get('ttl_s',.25)}
                    if spec.get('kind') not in ('joints','gripper','base'):raise ValueError('未知动作类型')
                    data={'action':app.actions.start(spec,self.sid)}
                elif path=='/api/presets':
                    p=dict(body)
                    if p.get('capture'):
                        snap=app.adapter.snapshot();names=body.get('joint_names') or sum(app.profile['groups'].values(),[])
                        if not isinstance(names,list) or not names:raise ValueError('请选择采集部位')
                        vals={}
                        for name in names:
                            item=snap.get('joints',{}).get(name,{})
                            if not finite(item.get('position')) or not finite(item.get('age_s')) or item['age_s']>app.profile.get('feedback_max_age_s',.5):raise ValueError('缺少新鲜反馈: '+str(name))
                            vals[name]=item['position']
                        p['joints']=vals;p['source']='measured_feedback'
                    row=app.presets.save(p);app.events.add('preset_saved','已保存预设',preset_id=row['id'],name=row['name']);data={'preset':row}
                elif path=='/api/presets/import':data={'presets':app.presets.import_many(body.get('presets'))}
                elif path=='/api/presets/delete':app.presets.delete(body.get('id'));data={}
                else:self.send(404,{'ok':False,'error':'接口不存在'});return
            self.send(200,dict(ok=True,**data))
        except ValueError as exc:self.send(409,{'ok':False,'error':str(exc)})
        except Exception as exc:self.send(500,{'ok':False,'error':str(exc)})

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--host',default='0.0.0.0');parser.add_argument('--port',type=int,default=4242);parser.add_argument('--profile',default=str(ROOT/'configs/f1-09.json'));parser.add_argument('--runtime',default=os.environ.get('F1_CONSOLE_RUNTIME',str(ROOT/'runtime')));parser.add_argument('--control-lock',default=os.environ.get('F1_CONTROL_LOCK','/home/user/Bruno/F1/runtime/control.lock'));parser.add_argument('--web-root');parser.add_argument('--offline',action='store_true');args=parser.parse_args()
    profile=json.loads(Path(args.profile).read_text());profile['profile_sha256']=hashlib.sha256(Path(args.profile).read_bytes()).hexdigest()
    app=App(profile,args.runtime,args.control_lock,args.offline,args.web_root)
    allowed=[f'{h}:{args.port}' for h in ('127.0.0.1','localhost',profile['ip'])]
    server=Server((args.host,args.port),app,allowed)
    def terminate(*_):threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,terminate);signal.signal(signal.SIGINT,terminate)
    print(f'F1 console serving on port {args.port}; state-only until explicit authenticated request',flush=True)
    try:server.serve_forever(poll_interval=.2)
    finally:app.close();server.server_close()
if __name__=='__main__':main()

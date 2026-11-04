#!/usr/bin/env python3
"""F1 local workbench: static hot reload, state, presets and explicit actions."""
from __future__ import annotations
import argparse,copy,subprocess,hashlib,http.cookies,json,mimetypes,os,secrets,signal,sys,threading,time,urllib.parse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from core import ActionManager,ControlLease,EventLog,OfflineAdapter,PresetStore,finite,utc
from data_service import DataService, ReviewConflict
from data_organization import OrganizationConflict
from data_delete import DataDeletion
from recording_service import RecordingService
from teleop_service import TeleopService
from hil_service import HILService
from hil_media import HILMedia
from hil_data import HILData
from replay_service import ReplayService
from startup_service import StartupService
from robot.adapter import ActionRejected
from preview_service import PreviewService,byte_range
from project_paths import console_allowed_hosts, installation_environment, profile_path, read_profile

ROOT=Path(__file__).resolve().parent
class App:
    def __init__(self,profile,runtime,control_lock,offline=False,web_root=None):
        self.web_root=Path(web_root or ROOT/'web').resolve()
        self.requested_profile=copy.deepcopy(profile)
        self.contract_verifier=None;self.contract_thread=None;self.adapter_factory=None
        self.profile=profile;self.runtime=Path(runtime);self.runtime.mkdir(parents=True,exist_ok=True)
        self.events=EventLog(self.runtime/'events.jsonl');self.presets=PresetStore(self.runtime/'presets.json',profile)
        self.health_cache={};self.health_at=0
        self.recording=RecordingService(self.runtime,ROOT,offline)
        self.teleop=TeleopService(self.runtime,ROOT,offline)
        self.hil=HILService(self.runtime,offline=offline)
        self.hil_data=HILData(self.hil)
        from recording.unified.catalog import CaptureCatalog
        self.capture_data=CaptureCatalog(self.runtime)
        self.startup=StartupService(profile,self.runtime,offline)
        self.data=DataService(profile.get('data_path',str(self.runtime/'raw_data')),self.runtime,self.web_root,extra_catalog=lambda:self.recording.catalog()+self.hil_data.catalog()+self.capture_data.catalog()+self.capture_data.datasets())
        self.preview=PreviewService(self.runtime,self.data,self.recording)
        self.sessions={};self.session_lock=threading.RLock();self.op_lock=threading.RLock();self.closed=threading.Event()
        self.contract={'ok':False,'scope':'offline_preview'}
        self.adapter=OfflineAdapter('本地开发预览，未连接ROS')
        if not offline:
            try:
                from robot.adapter import RosAdapter
                from robot.runtime_contract import verify_runtime_contract
                self.contract_verifier=verify_runtime_contract;self.adapter_factory=RosAdapter
                self.contract=verify_runtime_contract(profile)
                if not self.contract['ok']:
                    profile['motion_enabled']=False
                    self.events.add('contract_failed','动作保持关闭',errors=self.contract['errors'])
                self.adapter=RosAdapter(profile);self.adapter.start()
            except Exception as exc:
                self.events.add('adapter_error','ROS订阅未启动',error=str(exc));self.adapter=OfflineAdapter(str(exc))
        self.lease=ControlLease(control_lock);self.actions=ActionManager(self.adapter,self.events)
        self.watcher=threading.Thread(target=self._watch,name='lease-watch',daemon=True);self.watcher.start()
        if not offline and (isinstance(self.adapter,OfflineAdapter) or not self.contract['ok']):
            self.contract_thread=threading.Thread(target=self._wait_for_contract,name='startup-contract',daemon=True)
            self.contract_thread.start()
        self.hil_media=HILMedia(self.hil,lambda:bool(self.hil.state()['active']) or self.recording.state().get('mode') not in ('not_started','unavailable','idle','ready','review') or self.data.recording()['state']!='no_process_observed')
        self.data_delete=DataDeletion(self)
        self.replay=ReplayService(self)
        self.media_thread=threading.Thread(target=self._media_discovery,name='hil-video-discovery',daemon=True);self.media_thread.start()
        self.events.add('server_started','工作台已启动；未触发动作或采集',robot=profile['id'])
    def _media_discovery(self):
        while not self.closed.wait(10):
            try:
                with self.hil_media.lock:self.hil_media.scan()
            except Exception as exc:self.events.add('hil_video_discovery_error','介入视频任务发现失败',error=str(exc))

    def _watch(self):
        while not self.closed.wait(.1):
            with self.op_lock:
                if self.lease.fd and (self.lease.deadline<=time.monotonic() or self.lease.stopping):
                    self.lease.stopping=True
                    if self.actions.stop():
                        self.lease.release();self.events.add('control_released','控制权到期或释放，动作已停止')
    def _wait_for_contract(self):
        # Separate from the lease watchdog: ROS parameter checks can take seconds.
        while not self.closed.wait(10):
            if self._retry_contract_once():return
    def _retry_contract_once(self):
        with self.op_lock:
            if self.closed.is_set():return True
            reconnect=isinstance(self.adapter,OfflineAdapter)
            if self.contract['ok'] and not reconnect:return True
            if self.lease.fd is not None or self.actions.busy():return False
        try:
            result=self.contract_verifier(copy.deepcopy(self.requested_profile))
        except Exception as exc:
            result={'ok':False,'errors':[str(exc)],'scope':'startup_contract_retry'}
        candidate=None;adapter_error=None
        if reconnect and self.adapter_factory is not None:
            # Always create subscriptions with motion disabled. Never acquire a
            # lease, create command publishers or replay a previous request.
            try:
                profile=copy.deepcopy(self.requested_profile);profile['motion_enabled']=False
                candidate=self.adapter_factory(profile);candidate.start()
            except Exception as exc:
                adapter_error=str(exc)
                if candidate is not None:
                    try:candidate.close()
                    except Exception:pass
                candidate=None
        try:
            with self.op_lock:
                if self.closed.is_set():return True
                # Operator state may have changed during the blocking checks.
                if self.lease.fd is not None or self.actions.busy():return False
                self.contract=result
                if reconnect:
                    if candidate is None:
                        if adapter_error:
                            self.adapter=OfflineAdapter(adapter_error);self.actions.adapter=self.adapter
                        return False
                    old=self.adapter;self.adapter=candidate;self.actions.adapter=candidate;candidate=None
                    old.close()
                    self.events.add('adapter_ready','ROS订阅已自动恢复；未触发动作或采集')
                if not result['ok']:return False
                enabled=bool(self.requested_profile.get('motion_enabled',False))
                self.profile['motion_enabled']=enabled
                self.adapter.profile['motion_enabled']=enabled
                self.events.add('contract_ready','基础服务就绪，启动合同重新核验通过；未执行动作',motion_configured=enabled)
                return True
        finally:
            if candidate is not None:candidate.close()
    def session(self,raw):
        cookies=http.cookies.SimpleCookie()
        try:cookies.load(raw or '')
        except http.cookies.CookieError:pass
        key=cookies.get('f1_console_session');sid=key.value if key else ''
        with self.session_lock:
            if sid not in self.sessions:
                # A browser identity isolates leases; it is not a password gate.
                sid=secrets.token_urlsafe(24);self.sessions[sid]={'authenticated':True,'created':time.monotonic()}
            return sid,self.sessions[sid]
    def state(self,sid):
        s=self.adapter.snapshot();s.update(robot_id=self.profile['id'],control=self.lease.snapshot(sid),action=self.actions.latest(),authenticated=self.sessions[sid]['authenticated'],capabilities=self.capabilities())
        s['system_health']=self.health();s['services']=s['system_health'].get('services',{})
        s['pico']={'mode':'upper_body_only','start_script':str(Path(installation_environment(self.runtime)['PICO_UPPER_ROOT'])/'start.sh'),'status':'运行中' if s['services'].get('pico_upper',{}).get('running') else '未运行','version':'meaxiao 1.4.4 / upper-v2','commands':{k:str(Path(installation_environment(self.runtime)['PICO_UPPER_ROOT'])/(k+'.sh')) for k in ('check','start','stop')},'reason':'PCService已运行' if s['services'].get('pico_pc_service',{}).get('running') else 'PCService未运行；现场单独启动后再试遥操'}
        s['data']={'local_path':self.profile.get('data_path'),'mode':'isolated_lerobot_upper16','recording_control_enabled':not self.recording.offline,'transfer_configured':False,'review_enabled':True,'message':'LXL原始记录只读；新采集使用独立LeRobot上肢分支，未配置上传'}
        s['control']['password_required']=False
        s['control']['owner_label']=self.sessions.get(self.lease.owner,{}).get('owner') if self.lease.owner else s['control'].get('owner')
        return s
    def capabilities(self):
        capabilities=self.adapter.capabilities()
        capabilities['recording']={'available':not self.recording.offline,'reason':'独立LeRobot上肢录制；就绪条件见/api/recording/state，LXL流程不变','contract':'f1_upper_16d_v1','transfer_configured':False}
        return capabilities
    def health(self):
        if time.monotonic()-self.health_at>5:
            try:
                from health import snapshot
                self.health_cache=snapshot(self.profile.get("data_path",str(self.runtime/"raw_data")))
            except Exception as exc:self.health_cache={"error":str(exc)}
            self.health_at=time.monotonic()
        return self.health_cache
    def close(self):
        self.closed.set();self.actions.stop(wait=3)
        if not self.actions.busy():self.lease.release()
        self.adapter.close()
    def revision(self):
        committed=self.web_root.parent/'sync.json'
        if committed.is_file():
            return hashlib.sha256(committed.read_bytes()).hexdigest()[:16]
        h=hashlib.sha256()
        for p in sorted(self.web_root.rglob('*')):
            if p.is_file():
                st=p.stat();h.update(str(p.relative_to(self.web_root)).encode());h.update(f'{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}'.encode())
        return h.hexdigest()[:16]

class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,app,allowed):
        self.app=app;self.allowed=set(allowed);super().__init__(address,Handler)

class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args):pass
    def send(self,status,data,content_type='application/json; charset=utf-8',headers=None):
        b=(data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False,allow_nan=False).encode() if content_type.startswith('application/json') else data)
        self.send_response(status);self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(b)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY');self.send_header('Referrer-Policy','same-origin')
        for key,value in (headers or {}).items():self.send_header(key,str(value))
        if getattr(self,'sid',None):self.send_header('Set-Cookie',f'f1_console_session={self.sid}; HttpOnly; SameSite=Strict; Path=/')
        self.end_headers()
        try:self.wfile.write(b)
        except (BrokenPipeError,ConnectionResetError):pass
    def send_file(self,path,content_type):
        # Resolve/authorize the source before this method; keep the opened file
        # stable across replacement so validators and body describe one version.
        with path.open('rb') as source:
            stat=os.fstat(source.fileno());size=stat.st_size
            identity=f'{stat.st_dev}:{stat.st_ino}:{size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}'
            etag='"'+hashlib.sha256(identity.encode()).hexdigest()+'"'
            validators={'ETag':etag,'Cache-Control':'private, no-cache','Accept-Ranges':'bytes'}
            condition=self.headers.get('If-None-Match')
            if condition and any(tag.strip().removeprefix('W/') in ('*',etag) for tag in condition.split(',')):
                self.send_response(304)
                for key,value in validators.items():self.send_header(key,value)
                self.end_headers();return
            requested=self.headers.get('Range')
            # A stale/weak/date If-Range falls back to a full current body. Never
            # combine bytes from different derived-video versions in the cache.
            if self.headers.get('If-Range') not in (None,etag):requested=None
            try:start,end,partial=byte_range(requested,size)
            except ValueError:
                self.send_response(416);self.send_header('Content-Range',f'bytes */{size}');self.send_header('Content-Length','0');self.end_headers();return
            self.send_response(206 if partial else 200);self.send_header('Content-Type',content_type)
            length=end-start+1 if size else 0
            self.send_header('Content-Length',str(length))
            for key,value in validators.items():self.send_header(key,value)
            self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY')
            if partial:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
            self.end_headers();source.seek(start)
            try:
                while length:
                    chunk=source.read(min(length,256*1024))
                    if not chunk:break
                    self.wfile.write(chunk);length-=len(chunk)
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
            if path=='/api/session':data={'authenticated':True,'password_required':False}
            elif path=='/api/robot':data={'robot':dict(app.profile,capabilities=app.capabilities())}
            elif path=='/api/state':data=app.state(self.sid)
            elif path=='/api/replay/review':
                query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                data={'review':app.replay.review(self.sid,query.get('episode_id',[''])[0],
                      float(query.get('start_s',['0'])[0]),
                      float(query['end_s'][0]) if 'end_s' in query else None)}
            elif path=='/api/pose/preview':
                query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query);spec=json.loads(query.get('spec',['{}'])[0])
                if not isinstance(spec,dict) or spec.get('kind')!='pose':raise ValueError('姿态请求无效')
                if not hasattr(app.adapter,'plan'):raise ValueError('机器人未接入')
                data={'plan':app.adapter.plan(spec)}
            elif path=='/api/pose/review':
                from robot.pose_review import review
                query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query);spec=json.loads(query.get('spec',['{}'])[0])
                if not isinstance(spec,dict) or spec.get('kind')!='pose':raise ValueError('姿态请求无效')
                if not hasattr(app.adapter,'_common'):raise ValueError('机器人未接入')
                data={'review':review(app.adapter,spec)}
            elif path=='/api/waist/preview':
                query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query);delta=float(query.get('delta_m',['0'])[0])
                if not hasattr(app.adapter,'plan'):raise ValueError('机器人未接入')
                data={'plan':app.adapter.plan({'kind':'waist_height','delta_m':delta})}
            elif path=='/api/presets':data={'presets':app.presets.all()}
            elif path=='/api/events':data={'events':app.events.list()}
            elif path=='/api/replay/reports':
                query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                if query.get('limit',['1']) != ['1']:raise ValueError('只支持最近一条报告')
                data={'reports':app.replay.recent(query.get('episode_id',[''])[0])}
            elif path.startswith('/api/replay/reports/'):
                data={'report':app.replay.report(path.rsplit('/',1)[-1])}
            elif path=='/api/dev/revision':data={'revision':app.revision()}
            elif path=='/api/startup/state':data={'startup':app.startup.state()}
            elif path=='/api/storage':data={'health':app.health()}
            elif path=='/api/hil/state':data={'hil':app.hil.state()}
            elif path=='/api/hil/routes':data={'routes':app.hil.routes()}
            elif path=='/api/hil/config':data={'config':app.hil.next_config()}
            elif path=='/api/hil/interventions':data={'jobs':app.hil_media.listing()}
            elif path.startswith('/api/hil/video/'):
                parts=path.split('/')
                if len(parts)!=6:raise ValueError('invalid media path')
                self.send_file(app.hil_media.video(parts[4],parts[5]),'video/mp4');return
            elif path=='/api/teleop/state':data={'teleop':app.teleop.state(self.sid)}
            elif path=='/api/recording/state':data={'recording':app.recording.state()}
            elif path.startswith('/api/recording/review/') and path.endswith('.mp4'):
                self.send_file(app.recording.review_video(path.rsplit('/',1)[-1][:-4]),'video/mp4');return
            elif path.startswith(('/api/data/hil/', '/api/data/capture/')):
                parts=path.split('/')
                if len(parts)!=6:raise ValueError('invalid HIL replay path')
                replay=app.capture_data if parts[3]=='capture' else app.hil_data
                if parts[5]=='timeline':data=replay.timeline(parts[4])
                else:
                    query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    image,meta=replay.frame(parts[4],parts[5].removesuffix('.jpg'),float(query.get('t',['0'])[0]))
                    self.send(200,image,'image/jpeg',headers={'X-F1-Observation-Id':meta['observation_id'],'X-F1-Recording-Time':meta['time_s']});return
            elif path.startswith('/api/data/video/') and path.endswith('.mp4'):
                parts=path.split('/')
                if len(parts)!=6:raise ValueError('视频路径无效')
                self.send_file(app.preview.video(parts[4],parts[5][:-4]),'video/mp4');return
            elif path.startswith('/api/data/analytics/'):
                self.send_file(app.preview.analytics(path.rsplit('/',1)[-1]),'application/json; charset=utf-8');return
            elif path in ('/api/data','/api/data/catalog'):data=app.data.catalog()
            elif path=='/api/data/recording':data={'recording':app.data.recording()}
            elif path.startswith('/api/data/episodes/'):data={'episode':app.data.episode(path.rsplit('/',1)[-1])}
            elif path=='/api/system':data={'robot':app.profile['id'],'version':app.profile['version'],'runtime':str(app.runtime),'data_mode':'isolated_lerobot_upper16','upstream_ui':'http://'+app.profile.get('ip','127.0.0.1')+'/','motion_capabilities':app.capabilities(),'password_required':False,'hot_reload':True,'contract':app.contract,'ros_distro':'Jazzy','mode':'以本机动作能力检查为准'}
            elif path.startswith('/api/actions/'):data={'action':app.actions.get(path.rsplit('/',1)[-1])}
            elif path.startswith('/api/cameras/') and path.endswith('.jpg'):
                name=path.rsplit('/',1)[-1][:-4];frame=getattr(app.adapter,'preview_camera',app.adapter.camera)(name)
                if not frame or frame[2]>2:self.send(503,{'ok':False,'error':'相机没有新鲜图像'});return
                self.send(200,frame[0],frame[1],headers={'X-F1-Frame-Id':frame[3] if len(frame)>3 else hashlib.sha256(frame[0]).hexdigest(), 'X-F1-Frame-Age-Ms':round(frame[2]*1000,3)});return
            elif path.startswith('/api/'):self.send(404,{'ok':False,'error':'接口不存在'});return
            else:
                target=(app.web_root/('index.html' if path=='/' else urllib.parse.unquote(path).lstrip('/'))).resolve()
                if not target.is_relative_to(app.web_root) or not target.is_file():self.send(404,{'ok':False,'error':'文件不存在'});return
                self.send(200,target.read_bytes(),mimetypes.guess_type(str(target))[0] or 'application/octet-stream');return
            self.send(200,dict(ok=True,**data))
        except (ValueError,ActionRejected) as exc:self.send(400,{'ok':False,'error':str(exc)})
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
            # Compatibility endpoint for existing workers: no credentials required.
            self.send(200,{'ok':True,'authenticated':True,'password_required':False});return
        if path=='/api/control/acquire':
            try:
                owner=body.get('owner','现场操作者')
                if not isinstance(owner,str) or not 1<=len(owner.strip())<=80 or any(ord(ch)<32 for ch in owner):
                    raise ValueError('操作者名称须为 1–80 字符')
                with app.op_lock:
                    token=app.lease.acquire(self.sid)
                    self.session_data.update(authenticated=True,owner=owner.strip())
                    app.events.add('control_acquired','浏览器取得研发控制权',owner=owner.strip())
                self.send(200,{'ok':True,'authenticated':True,'lease_id':token,'expires_in_s':app.lease.ttl});return
            except ValueError as exc:
                self.send(409,{'ok':False,'error':str(exc)});return
            except Exception:
                self.send(500,{'ok':False,'error':'控制会话暂不可用'});return
        if path in ('/api/replay/start','/api/replay/align'):
            try:
                data=app.replay.start(self.sid,body,align=path.endswith('/align'))
                self.send(200,{'ok':True,'action':data})
            except (ValueError,RuntimeError,OSError) as exc:self.send(409,{'ok':False,'error':str(exc)})
            return
        if path=='/api/hil/finish':
            try:
                data=app.hil.finish(body)
                app.events.add('hil_finish_requested','现场请求结束HIL会话并保存',run_id=data['run_id'])
                self.send(200,dict(ok=True,**data))
            except (ValueError,OSError) as exc:self.send(409,{'ok':False,'error':str(exc)})
            return
        if path.startswith('/api/hil/media/'):
            try:
                op=path.rsplit('/',1)[-1]
                if op=='source':data=app.hil_media.source(body.get('id',''),body.get('lease'),body.get('name'),body.get('offset'),body.get('count'))
                elif op=='finish':data=app.hil_media.finish(body)
                else:data=app.hil_media.command(op,body)
                self.send(200,dict(ok=True,**data))
            except Exception as exc:self.send(409,{'ok':False,'error':str(exc)})
            return
        if path in ('/api/hil/config','/api/hil/policy/check','/api/hil/policy/probe'):
            try:
                if path=='/api/hil/config':data=app.hil.save_config(body)
                elif path.endswith('/check'):data=app.hil.check_policy()
                else:data=app.hil.probe(app.adapter,app.profile)
                self.send(200,dict(ok=True,**data))
            except Exception as exc:self.send(409,{'ok':False,'error':str(exc)})
            return
        if path.startswith('/api/teleop/'):
            try:
                operation=path.rsplit('/',1)[-1]
                if operation=='prepare':
                    with app.op_lock:
                        if app.lease.fd is not None or app.actions.busy():raise ValueError('请先结束网页动作并释放姿态控制权')
                        data=app.teleop.prepare(body.get('variant'),self.sid)
                elif operation=='confirm':
                    with app.op_lock:
                        if app.lease.fd is not None or app.actions.busy():raise ValueError('网页动作会话尚未释放')
                        data=app.teleop.command(operation,self.sid,body.get('session_id'),body.get('nonce'))
                elif operation=='stop':data=app.teleop.command(operation,self.sid,body.get('session_id'))
                else:raise ValueError('未知遥操操作')
                app.events.add('teleop_operation','网页遥操会话操作',operation=operation,variant=data.get('variant'),phase=data.get('phase'))
                self.send(200,{'ok':True,'teleop':data})
            except (ValueError,OSError,subprocess.SubprocessError) as exc:self.send(409,{'ok':False,'error':str(exc)})
            return
        if path.startswith('/api/recording/'):
            try:
                recording=app.recording.command(path.rsplit('/',1)[-1]);app.data.cache_at=0
                app.events.add('recording_operation','独立录制操作',operation=path.rsplit('/',1)[-1],mode=recording.get('mode'))
                self.send(200,{'ok':True,'recording':recording})
            except ValueError as exc:self.send(409,{'ok':False,'error':str(exc),'recording':app.recording.state()})
            return
        if path=='/api/data/organization':
            try:
                result=app.data.organization.command(body,lambda:app.data.catalog(refresh=True))
                app.events.add('data_organization_saved','已保存版本分类',operation=body.get('operation'))
                self.send(200,{'ok':True,'organization':result})
            except OrganizationConflict as exc:
                self.send(409,{'ok':False,'error':str(exc),'code':exc.code,'organization':exc.organization})
            except (ValueError,TypeError,KeyError) as exc:
                self.send(400,{'ok':False,'error':str(exc)})
            except OSError:
                self.send(500,{'ok':False,'error':'版本目录保存失败，请重新读取核对'})
            return
        if path=='/api/data/delete':
            try:
                result=app.data_delete.delete(body,self.session_data.get('owner','已登录操作者'))
                self.send(200,result)
            except (ValueError,OSError) as exc:self.send(409,{'ok':False,'error':str(exc)})
            return
        if path=='/api/data/notes':
            try:
                row=app.data.save_notes(body.get('episode_id'),body.get('notes',''),self.session_data.get('owner','已登录操作者'),body.get('expected_source_signature'),body.get('expected_review_revision'))
                app.events.add('data_notes_saved','已保存录制备注',episode_id=row['episode_id'])
                self.send(200,{'ok':True,'review':row})
            except ReviewConflict as exc:self.send(409,{'ok':False,'error':str(exc),'code':exc.code,'review':exc.review})
            except ValueError as exc:self.send(400,{'ok':False,'error':str(exc)})
            except Exception as exc:self.send(500,{'ok':False,'error':str(exc)})
            return
        if path=='/api/data/reviews':
            try:
                row=app.data.save_review(body.get('episode_id'),body.get('status'),body.get('notes',''),self.session_data.get('owner','已登录操作者'),body.get('expected_source_signature'),body.get('expected_review_revision'))
                app.events.add('data_review_saved','已保存独立数据审核',episode_id=row['episode_id'],status=row['status'])
                self.send(200,{'ok':True,'review':row})
            except ReviewConflict as exc:self.send(409,{'ok':False,'error':str(exc),'code':exc.code,'review':exc.review})
            except ValueError as exc:self.send(409,{'ok':False,'error':str(exc)})
            except Exception as exc:self.send(500,{'ok':False,'error':str(exc)})
            return
        try:
            with app.op_lock:
                if path=='/api/startup/start':
                    if body.get('confirm') is not True:raise ValueError('需要明确确认启动基础节点')
                    if app.lease.fd is not None or app.actions.busy():raise ValueError('请先结束动作并释放控制权')
                    if app.teleop.state(self.sid).get('phase') not in ('idle','failed','stopped'):raise ValueError('请先停止 Pico 会话')
                    data={'startup':app.startup.start()};app.events.add('base_start_requested','现场操作者确认启动原 start_all.sh')
                elif path=='/api/logout':
                    app.replay.invalidate(self.sid)
                    if app.lease.owner==self.sid:app.lease.stopping=True;app.actions.stop()
                    data={'authenticated':True,'password_required':False}
                elif path in ('/api/control/heartbeat','/api/control/release','/api/stop'):
                    app.lease.require(self.sid,body.get('lease_id'),renew=path.endswith('heartbeat'))
                    if path.endswith('heartbeat'):data={'expires_in_s':app.lease.ttl}
                    else:
                        app.replay.invalidate(self.sid)
                        done=app.actions.stop()
                        if path.endswith('release'):
                            app.lease.stopping=True
                            if done:app.lease.release()
                        data={'stopped':done,'action':app.actions.latest()}
                elif path=='/api/actions/plan':
                    app.lease.require(self.sid,body.get('lease_id'))
                    if not hasattr(app.adapter,'plan'):raise ValueError('未连接机器人，不能规划动作')
                    data={'plan':app.adapter.plan(body)}
                elif path=='/api/actions':
                    app.lease.require(self.sid,body.get('lease_id'),renew=True)
                    spec={k:v for k,v in body.items() if k not in ('lease_id',)}
                    if spec.get('kind')=='base_jog':spec={'kind':'base',**spec.get('targets',{}),'ttl_s':spec.get('ttl_s',.25)}
                    if spec.get('kind') not in ('joints','gripper','base','lift','waist_height','pose'):raise ValueError('未知动作类型')
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
        except (ValueError,ActionRejected) as exc:self.send(409,{'ok':False,'error':str(exc)})
        except Exception as exc:self.send(500,{'ok':False,'error':str(exc)})

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--host',default='0.0.0.0');parser.add_argument('--port',type=int,default=4242);parser.add_argument('--profile',default=str(profile_path()));parser.add_argument('--runtime',default=installation_environment()['F1_CONSOLE_RUNTIME']);parser.add_argument('--control-lock');parser.add_argument('--web-root');parser.add_argument('--offline',action='store_true');args=parser.parse_args()
    args.profile=str(Path(args.profile).expanduser().resolve())
    args.runtime=str(Path(args.runtime).expanduser().resolve())
    args.control_lock=str(Path(args.control_lock or os.environ.get('F1_CONTROL_LOCK') or str(Path(args.runtime)/'control.lock')).expanduser().resolve())
    os.environ.update(F1_PROFILE_PATH=args.profile, F1_CONSOLE_RUNTIME=args.runtime, F1_CONTROL_LOCK=args.control_lock, F1_CONSOLE_URL=f'http://127.0.0.1:{args.port}')
    os.environ.update(installation_environment(args.runtime))
    profile=read_profile(args.profile);profile['profile_sha256']=hashlib.sha256(Path(args.profile).read_bytes()).hexdigest()
    if not args.offline:
        ros_logs=Path(args.runtime)/'ros_logs'
        ros_logs.mkdir(parents=True,exist_ok=True)
        os.environ['ROS_LOG_DIR']=str(ros_logs)
    app=App(profile,args.runtime,args.control_lock,args.offline,args.web_root)
    allowed=console_allowed_hosts(profile,args.port)
    server=Server((args.host,args.port),app,allowed)
    def terminate(*_):threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,terminate);signal.signal(signal.SIGINT,terminate)
    print(f'F1 console serving on port {args.port}; state-only until an explicit operator request',flush=True)
    try:server.serve_forever(poll_interval=.2)
    finally:app.close();server.server_close()
if __name__=='__main__':main()

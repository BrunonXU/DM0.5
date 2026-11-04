"""Lifecycle/contracts/derived media tests use synthetic feedback; zero outputs."""
import base64
import copy
from dataclasses import replace
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import pytest
ROOT=Path(__file__).resolve().parents[1]
DAGGER=ROOT.parent/'DAgger'
if not (DAGGER/'F1/src/f1dagger').is_dir():
    pytest.skip('cross-project integration fixture requires DAgger development checkout',allow_module_level=True)
for p in (ROOT,DAGGER/'F1/src',DAGGER/'common/src',DAGGER/'F1/services'):
    sys.path.insert(0,str(p))
from hil_contract import validate_session,capabilities,validate_health,validate_actions,NAMES,CONTRACT
from hil_media import HILMedia,digest
from hil_service import HILService
from server import App,Server
from robot.adapter import StateStore
from f1dagger.recording import F1Recorder,make_sink
from f1dagger.observation import Observation
from dagger_core.recording_observation import TimestampedFrame,DeviceTimestamp
from f1dagger.interventions import enqueue_session,intervention_windows
from video_worker import Client,process

def test_portable_contract_copies_and_bad_capabilities():
    assert (ROOT/'hil_contract.py').read_bytes()==(DAGGER/'F1/src/f1dagger/session.py').read_bytes()
    cfg=validate_session({})['policy'];h=capabilities('test')
    assert validate_health(h,cfg)==h
    for k,v in [('units',['radian']*16),('frequency_hz',10),('chunk_steps',49),('action_type','delta')]:
        bad=copy.deepcopy(h);bad['capabilities'][k]=v
        with pytest.raises(ValueError):validate_health(bad,cfg)
    with pytest.raises(ValueError):validate_session({'policy':{'frequency_hz':10}})
    assert not validate_session({'purpose':'input_check','auto_video':True})['auto_video']

def test_snapshot_identity_expiry_no_legacy_green(tmp_path):
    root=tmp_path/'raw-root';root.mkdir();(tmp_path/'hil.json').write_text(json.dumps(dict(schema='f1_hil_panel_v1',runtime=str(root),launcher='/tmp/start',ssh_host='user@robot')))
    now=[100.];procs=[dict(pid=8,start_ticks='4',started_unix=90.)]
    service=HILService(tmp_path,clock=lambda:now[0],process_reader=lambda:procs)
    s=dict(schema='f1_hil_status_v1',pid=8,start_ticks='4',run_id='abc',updated_unix=100.,binding={},status=dict(state='hil_latched',input_fresh=True))
    path=root/'status.json';path.write_text(json.dumps(s))
    assert service.state()['summary_fresh']
    now[0]+=2.1;assert service.state()['phase']=='unknown'
    s['updated_unix']=103;s['start_ticks']='3';path.write_text(json.dumps(s));now[0]=103
    assert not service.state()['summary_fresh']

def test_preview_identity_does_not_advance_on_read():
    now=[1.];store=StateStore(clock=lambda:now[0]);msg=SimpleNamespace(data=b'\xff\xd8abc',format='jpeg',header=SimpleNamespace(stamp=SimpleNamespace(sec=1,nanosec=0)))
    store.update_camera('head',msg);first=store.preview_camera('head');now[0]=1.3
    second=store.preview_camera('head');assert first[3]==second[3] and second[2]==pytest.approx(.3)
    store.update_camera('head',msg);assert store.preview_camera('head')[3]!=first[3]
    assert len(store.camera('head'))==3

def fixture_run(root, purpose="policy_hil", camera_sizes=None):
    from PIL import Image
    from io import BytesIO
    run=root/'raw'/'synthetic-hil';run.mkdir(parents=True)
    sink=make_sink(run/'payload','hil-record')
    journal=F1Recorder(run/'journal',episode_id=run.name,task_prompt='synthetic only',binding=dict(execution_mode='shadow',synthetic=True,purpose=purpose,auto_video=True,model_id='mock',model_version='v1'))
    camera_sizes=camera_sizes or {c:(32,24) for c in ('head','left_wrist','right_wrist')}
    images={}
    for c,size in camera_sizes.items():
        io=BytesIO();Image.new('RGB',size,(40,120,200)).save(io,format='JPEG');images[c]=io.getvalue()
    for i in range(15):
        ns=1_000_000_000+i*33_333_333
        frames=tuple(TimestampedFrame(c,i,DeviceTimestamp(ns,'ns','synthetic'),ns+1,images[c],'image/jpeg',*camera_sizes[c]) for c in ('head','left_wrist','right_wrist'))
        o=Observation(i,ns,ns+10,frames,(0.,)*16,(ns+2,)*16,(0.,)*22)
        state='policy' if i<3 or i>=10 else 'hil_latched';source='policy' if state=='policy' else 'human_hold'
        assert sink.submit(o)
        assert journal.record_timestamped_observation(o,hil_state=state,intervention_id=1,policy_epoch=1)
        assert journal.control(now=ns+11,state=state,intervention=1,epoch=1,selected=[0.]*16,source=source,measured=[0.]*16,sequence=i,submitted=None)
    assert sink.close(status='completed');assert journal.close(status='completed')
    return run

def test_intervention_end_requires_actual_policy_output():
    obs=[{'available_monotonic_ns':i*1_000_000_000} for i in range(20)]
    control=[dict(monotonic_ns=2e9,hil_state='hil_latched',intervention_id=1,source='human'),dict(monotonic_ns=4e9,hil_state='handoff_hold',intervention_id=1,source='hold'),dict(monotonic_ns=6e9,hil_state='policy',intervention_id=1,source='hold'),dict(monotonic_ns=8e9,hil_state='policy',intervention_id=1,source='policy')]
    w=intervention_windows(obs,control,'policy_hil')[0]
    assert w['resume_ns']==8e9 and w['end_ns']==13e9 and w['truncated_before']
    control[3].update(hil_state='safe_hold',source='hold')
    w=intervention_windows(obs,control,'policy_hil')[0];assert w['resume_ns'] is None and w['abnormal_end']

def test_media_lease_pause_path_and_idempotence(tmp_path):
    root=tmp_path/'dagger';run=fixture_run(root);queue=root/'video-jobs'
    a=enqueue_session(run,queue);assert enqueue_session(run,queue)==a
    (tmp_path/'hil.json').write_text(json.dumps(dict(runtime=str(root))))
    busy=[False];media=HILMedia(HILService(tmp_path),lambda:busy[0]);job=media.claim()['job'];assert media.claim()['job'] is None
    busy[0]=True
    with pytest.raises(ValueError,match='paused'):media.source(a,job['lease'],'payload/head.pack',0,10)
    busy[0]=False
    with pytest.raises(ValueError):media.source(a,job['lease'],'../../secret',0,10)
    with pytest.raises(ValueError):media.source(a,'wrong','payload/head.pack',0,10)
    assert len(base64.b64decode(media.source(a,job['lease'],'payload/head.pack',0,10)['data']))==10

def test_end_to_end_server_worker_video_and_auth(tmp_path):
    import shutil
    if not shutil.which('ffmpeg'):pytest.skip('ffmpeg unavailable')
    runtime=tmp_path/'console';runtime.mkdir();root=tmp_path/'dagger';run=fixture_run(root)
    (runtime/'hil.json').write_text(json.dumps(dict(schema='f1_hil_panel_v1',runtime=str(root),launcher='/tmp/start',ssh_host='user@robot')))
    profile=json.loads((ROOT/'configs/robot.example.json').read_text())
    app=App(profile,runtime,tmp_path/'lock',offline=True);http=Server(('127.0.0.1',0),app,[]);addr='127.0.0.1:'+str(http.server_port);http.allowed.add(addr)
    t=threading.Thread(target=http.serve_forever,daemon=True);t.start()
    before={str(p.relative_to(run)):digest(p) for p in run.rglob('*') if p.is_file()}
    try:
        import http.client as hc
        connection=hc.HTTPConnection('127.0.0.1',http.server_port)
        connection.request('POST','/api/hil/media/claim',body='{}',headers={'Content-Type':'application/json','Origin':'http://wrong-origin.invalid','X-F1-Request':'1'})
        r=connection.getresponse();assert r.status==403;r.read();connection.close()
        legacy=tmp_path/'legacy-worker.token';legacy.write_text('legacy-worker-compatibility')
        client=Client(dict(robot_url='http://'+addr,access_token_file=str(legacy)))
        job=client.post('/api/hil/media/claim',{})['job'];assert job
        process(client,job,tmp_path/'server-cache')
        jobs=app.hil_media.listing();assert jobs[0]['status']=='ready' and len(jobs[0]['clips'])==1
        clip=jobs[0]['clips'][0];path=app.hil_media.video(job['id'],clip['name']);assert path.stat().st_size>1000
        client.op('review',job,clip=clip['name'],review='accepted')
        assert app.hil_media.listing()[0]['clips'][0]['training_ready'] is False
        assert client.post('/api/hil/media/claim',{})['job'] is None
        assert before=={str(p.relative_to(run)):digest(p) for p in run.rglob('*') if p.is_file()}
        connection=hc.HTTPConnection('127.0.0.1',http.server_port);connection.request('GET',clip['url'],headers={'Range':'bytes=0-15'});r=connection.getresponse();assert r.status==206 and r.read()[4:8]==b'ftyp';connection.close()
    finally:http.shutdown();http.server_close();app.close();t.join(2)

def test_mock_policy_health_and_live_observation_probe(tmp_path):
    import os,socket,subprocess
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    env=dict(os.environ,PYTHONPATH=str(DAGGER/'F1/src')+':'+str(DAGGER/'common/src'))
    model=subprocess.Popen([sys.executable,str(DAGGER/'F1/services/policy_server.py'),'--mock','--port',str(port)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    stop=threading.Event();store=StateStore();store.connected=True
    from PIL import Image
    from io import BytesIO
    stream=BytesIO();Image.new('RGB',(16,16)).save(stream,format='JPEG')
    def feedback():
        while not stop.is_set():
            header=SimpleNamespace(stamp=SimpleNamespace(sec=1,nanosec=0))
            store.update_joints(SimpleNamespace(name=NAMES[:14],position=[0.]*14,header=header))
            for side in ('left','right'):store.update_gripper(side,SimpleNamespace(name=[side+'_gripper_position'],position=[0.],header=header))
            for side in ('head','left','right'):store.update_camera(side,SimpleNamespace(data=stream.getvalue(),format='jpeg',header=header))
            stop.wait(.02)
    thread=threading.Thread(target=feedback,daemon=True);thread.start()
    try:
        service=HILService(tmp_path);config=service.next_config();config['policy']['endpoint']='http://127.0.0.1:'+str(port);service.save_config(config)
        deadline=time.monotonic()+3
        while True:
            try:service.check_policy();break
            except OSError:
                if time.monotonic()>deadline:raise
                time.sleep(.02)
        result=service.probe(SimpleNamespace(store=store),{'joint_limits_deg':{n:[-180,180] for n in NAMES[:14]}})
        assert result['shape']==[50,16] and not result['robot_commands'] and result['contract_valid']
        stop.set();thread.join(1)
        with pytest.raises(ValueError,match='state16'):service.probe(SimpleNamespace(store=store),{'joint_limits_deg':{n:[-180,180] for n in NAMES[:14]}})
    finally:stop.set();thread.join(1);model.terminate();model.wait(3)

@pytest.mark.parametrize('change',[{'actions':[[float('nan')]*16]*50},{'actions':[[True]*16]*50},{'actions':[[0]*15]*50},{'frequency_hz':10},{'model_version':'changed'},{'policy_epoch':3},{'actions':[[999]*16]*50}])
def test_probe_rejects_bad_policy_result(change):
    metadata=dict(contract=CONTRACT,request_id='x',observation_id=1,policy_epoch=2,observation_monotonic_ns=20,frequency_hz=30.,chunk_steps=50)
    response=dict(metadata,model_id='test',model_version='mock-v1',actions=[[0.]*16 for _ in range(50)]);response.update(change)
    with pytest.raises(ValueError):validate_actions(response,metadata,capabilities('test'),{n:[-180,180] for n in NAMES[:14]})


def test_v15_rollback_uses_new_console_not_previous_process_snapshot(tmp_path):
    import os
    root=tmp_path/'raw-root';root.mkdir()
    (tmp_path/'hil.json').write_text(json.dumps(dict(schema='f1_hil_panel_v1',runtime=str(root),launcher='/tmp/start',ssh_host='user@robot')))
    service=HILService(tmp_path,clock=lambda:100.,process_reader=lambda:[dict(pid=8,start_ticks='4',started_unix=90.)])
    old=dict(schema='f1_hil_status_v1',pid=7,start_ticks='3',run_id='old',updated_unix=85.,binding={},status=dict(state='policy',input_fresh=True))
    (root/'status.json').write_text(json.dumps(old))
    log=root/'onsite-powered-console.log'
    log.write_text('F1 IDENTITY '+json.dumps(dict(release='v15'))+'\nF1 STATUS '+json.dumps(dict(state='hil_latched',input_fresh=True,execution='POWERED'))+'\n')
    os.utime(log,(99.,99.))
    current=service.state()
    assert current['source']=='receiver_console_summary' and current['summary_fresh']
    assert current['phase']=='hil_latched' and current['release']=='v15'
    # A matching structured producer must not use a fresh log to mask expiry.
    old.update(pid=8,start_ticks='4',updated_unix=95.)
    (root/'status.json').write_text(json.dumps(old));service.cached_at=0
    assert service.state()['phase']=='unknown' and not service.state()['summary_fresh']


def test_play_episode_excluded_from_video_and_export(tmp_path):
    from f1dagger.recording import audit
    from f1dagger.export import select_segments
    run=fixture_run(tmp_path,purpose='teleop_play')
    assert enqueue_session(run,tmp_path/'video-jobs') is None
    report=audit(run);assert report['valid']
    review=dict(contract=CONTRACT,manifest_sha256=report['manifest_sha256'],reviewer='test',accepted_interventions=[1])
    with pytest.raises(ValueError,match='never expert training data'):select_segments(run,review)

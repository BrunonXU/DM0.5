"""All launchers are fixtures; no robot scripts or commands are executed."""
import hashlib,json,sys,time,threading,os,pty,subprocess
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from teleop_service import TeleopService,legacy_config,UNIT
from teleop.worker import Session,PROMPT,markers
from core import atomic_json
from test_data_auth import http_app

@pytest.fixture
def service(tmp_path,monkeypatch):
    root=tmp_path/'code';(root/'teleop').mkdir(parents=True)
    script=root/'fixture.sh';script.write_text('exit 0\n')
    (root/'teleop/config.json').write_text(json.dumps({'lxl':{'start':str(script),'start_sha256':hashlib.sha256(script.read_bytes()).hexdigest()}}))
    calls=[]
    monkeypatch.setattr('teleop_service.subprocess.run',lambda args,**kw:(calls.append(args) or SimpleNamespace(returncode=0)))
    return TeleopService(tmp_path/'runtime',root),calls

def test_get_never_launches_and_prepare_uses_fixed_unit(service):
    s,calls=service
    assert s.state()['phase']=='idle' and calls==[]
    r=s.prepare('lxl','browser')
    assert r['phase']=='starting' and len(calls)==1
    assert '--unit='+UNIT in calls[0] and '--property=Restart=no' in calls[0]
    assert not (s.current()/'confirm.json').exists()
    with pytest.raises(ValueError):s.prepare('lxl','browser')

@pytest.mark.parametrize('variant',['shell',[],None,'../../other'])
def test_no_arbitrary_variant(service,variant):
    s,calls=service
    with pytest.raises(ValueError):s.prepare(variant,'a')
    assert calls==[]

def test_offline_and_script_changes_do_not_launch(service):
    s,calls=service;s.offline=True
    with pytest.raises(ValueError):s.prepare('lxl','a')
    s.offline=False;Path(s.config['lxl']['start']).write_text('changed')
    with pytest.raises(ValueError):s.prepare('lxl','a')
    assert calls==[]

def waiting(s):
    s.prepare('lxl','owner');folder=s.current()
    atomic_json(folder/'status.json',{'phase':'awaiting_confirmation','checked_unix':time.time(),'nonce':'n'*48})
    return folder

def test_confirmation_is_owner_scoped_single_use_and_cancelable(service):
    s,_=service;folder=waiting(s)
    assert 'nonce' not in s.state('other')
    for owner,nonce in [('other','n'*48),('owner','wrong')]:
        with pytest.raises(ValueError):s.command('confirm',owner,folder.name,nonce)
    with pytest.raises(ValueError):s.command('confirm','owner','changed','n'*48)
    s.command('confirm','owner',folder.name,'n'*48)
    assert not s.state('owner')['can_confirm']
    with pytest.raises(ValueError):s.command('confirm','owner',folder.name,'n'*48)
    s.command('stop','other-authenticated-browser',folder.name)
    assert (folder/'stop.json').exists()

def test_stale_state_cannot_confirm(service):
    s,_=service;folder=waiting(s)
    atomic_json(folder/'status.json',{'phase':'awaiting_confirmation','checked_unix':time.time()-30,'nonce':'n'*48})
    assert s.state('owner')['phase']=='failed'
    with pytest.raises(ValueError):s.command('confirm','owner',folder.name,'n'*48)

def test_legacy_config_never_executes_code(tmp_path):
    p=tmp_path/'config.py';p.write_text("raise RuntimeError('do not execute')\nlocal_ip='192.0.2.3'\ntarget_left=[1]*7\nreset_left=[1,2,3]\n")
    assert legacy_config(p)=={'local_ip':'192.0.2.3','reset_left':[1,2,3]}

def test_http_auth_csrf_lease_and_stop_separation(http_app,monkeypatch):
    app,request=http_app;calls=[]
    monkeypatch.setattr(app.teleop,'prepare',lambda *a:(calls.append(a) or {'phase':'preparing'}))
    monkeypatch.setattr(app.teleop,'command',lambda *a:(calls.append(a) or {'phase':'stopping'}))
    _,_,cookie=request('/api/session')
    assert request('/api/teleop/state',cookie=cookie)[0]==200 and calls==[]
    assert request('/api/teleop/prepare',{'variant':'lxl'},cookie,origin=False)[0]==403
    request('/api/login',{},cookie)
    assert request('/api/teleop/prepare',{'variant':'lxl'},cookie,origin=False)[0]==403
    assert request('/api/teleop/prepare',{'variant':'lxl'},cookie)[0]==200
    request('/api/control/acquire',{},cookie)
    assert request('/api/teleop/prepare',{'variant':'lxl'},cookie)[0]==409
    assert request('/api/teleop/confirm',{'session_id':'x','nonce':'n'},cookie)[0]==409
    assert request('/api/teleop/stop',{'session_id':'x'},cookie)[0]==200

def fake_session(tmp_path):
    s=Session.__new__(Session);s.folder=tmp_path;s.state={'phase':'preparing'};s.stopping=False;s.child=None;s.last_publish=0;s.last_phase=None
    return s

def test_real_pty_receives_enter_only_after_explicit_confirmation(tmp_path):
    s=fake_session(tmp_path);master,slave=pty.openpty();received=tmp_path/'received'
    child=subprocess.Popen([sys.executable,'-c',"import sys,pathlib;print('"+PROMPT+"',flush=True);sys.stdin.readline();pathlib.Path(sys.argv[1]).touch()",str(received)],stdin=slave,stdout=slave,stderr=slave)
    os.close(slave);s.child=child
    try:
        assert PROMPT.encode() in os.read(master,8192)
        result=[]
        def waiter():
            result.append(s.await_confirmation())
            if result[-1]:os.write(master,b'\n')
        thread=threading.Thread(target=waiter);thread.start()
        deadline=time.time()+3
        while not (tmp_path/'status.json').exists() and time.time()<deadline:time.sleep(.02)
        time.sleep(.2);assert not received.exists()
        nonce=json.loads((tmp_path/'status.json').read_text())['nonce']
        atomic_json(tmp_path/'confirm.json',{'nonce':nonce})
        thread.join(3);child.wait(3)
        assert result==[True] and received.exists() and s.state['nonce'] is None
    finally:
        if child.poll() is None:child.terminate();child.wait(3)
        os.close(master)

def test_cancel_does_not_acknowledge_prompt(tmp_path):
    s=fake_session(tmp_path);(tmp_path/'stop.json').touch()
    assert s.await_confirmation() is False

def test_markers_match_argv_not_substring():
    assert not markers([b'python',b'-c',b'print("teleop_lxl.py")'])
    assert markers([b'python',b'/home/user/lxl/teleop_lxl.py'])=={'lxl_teleop'}

def test_stop_signals_only_owned_pid_and_preserves_external(tmp_path):
    own=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
    external=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
    s=fake_session(tmp_path);s.child=own
    s.scoped_pids=lambda:{own.pid} if own.poll() is None else set()
    try:
        s.stop_owned()
        assert own.poll() is not None and external.poll() is None
    finally:
        for process in (own,external):
            if process.poll() is None:process.terminate()
            process.wait(3)

def test_stop_prevents_launch_even_with_valid_script(tmp_path):
    s=fake_session(tmp_path);s.stopping=True
    with pytest.raises(ValueError,match='取消'):s.launch_pty()
    assert s.child is None

def test_changed_script_cannot_launch_after_confirmation(tmp_path):
    s=fake_session(tmp_path);script=tmp_path/'script.sh';script.write_text('exit 0')
    s.cfg={'start':str(script),'start_sha256':'not-the-original-hash'}
    with pytest.raises(ValueError,match='发生变化'):s.launch_pty()
    assert s.child is None

def test_external_pcservice_reused_without_spawning(tmp_path,monkeypatch):
    s=fake_session(tmp_path)
    monkeypatch.setattr('teleop.worker.observed',lambda:{123:{'pcservice'}})
    monkeypatch.setattr('teleop.worker.subprocess.Popen',lambda *a,**kw:pytest.fail('must reuse existing PCService'))
    s.prepare_pcservice()

def test_confirmation_cancel_blocks_later_confirm(service):
    s,_=service;folder=waiting(s);s.command('stop','owner',folder.name)
    assert not s.state('owner')['can_confirm']
    with pytest.raises(ValueError):s.command('confirm','owner',folder.name,'n'*48)

def test_hardware_checks_all_feedback_and_right_gripper(monkeypatch):
    from teleop.worker import check_hardware
    import io
    names=[f'arm_{side}_j{i}' for side in ('l','r') for i in range(1,8)]+[f'head_j{i}' for i in range(1,4)]+[f'body_j{i}' for i in range(1,6)]
    state={'capabilities':{'joints':{'available':True},'gripper':{'available':True}},'graph_age_s':.1,'graph':{'right_gripper_command':{'publishers':[]}},'joints':{n:{'age_s':.01} for n in names},'grippers':{n:{'age_s':.01} for n in ('left','right')}}
    monkeypatch.setattr('teleop.worker.urllib.request.urlopen',lambda url,**kw:io.BytesIO(json.dumps({'contract':{'ok':True}} if url.endswith('system') else state).encode()))
    assert check_hardware()['graph_age_s']==.1
    state['graph']['right_gripper_command']['publishers']=[{'name':'other'}]
    with pytest.raises(ValueError,match='右夹爪'):check_hardware()
    state['graph']['right_gripper_command']['publishers']=[]
    state['joints']['body_j5']['age_s']=.4
    with pytest.raises(ValueError,match='完整关节'):check_hardware()


def test_stop_refuses_pid_that_left_owned_scope(tmp_path,monkeypatch):
    s=fake_session(tmp_path);calls=[];scopes=iter([{123},set(),set()]);s.scoped_pids=lambda:next(scopes,set())
    monkeypatch.setattr('teleop.worker.os.pidfd_open',lambda pid:999)
    monkeypatch.setattr('teleop.worker.os.close',lambda fd:calls.append(('close',fd)))
    monkeypatch.setattr('teleop.worker.signal.pidfd_send_signal',lambda *args:calls.append(('signal',args)))
    s.stop_owned()
    assert calls==[('close',999)]


def test_direct_start_is_bound_to_explicit_request_and_can_be_cancelled(service,tmp_path):
    gateway,calls=service
    assert gateway.state()['phase']=='idle' and not calls
    gateway.prepare('lxl','browser')
    request=json.loads((gateway.current()/'request.json').read_text())
    assert request['direct_start'] is True and request['id']==gateway.current().name
    s=fake_session(tmp_path);s.request=request
    assert s.await_confirmation() is True
    assert s.state['phase']=='starting' and s.state['nonce'] is None
    (tmp_path/'stop.json').touch()
    assert s.await_confirmation() is False

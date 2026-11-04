import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

SOURCE=Path(__file__).resolve().parents[1]/'windows_client/scripts/windows_session_logs.py'
spec=importlib.util.spec_from_file_location('session_logs', SOURCE)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def packet(data=b'connection refused\n', offset=0, name='ssh.log'):
    return dict(session='a'*32,model='rlt400',files=[dict(name=name,offset=offset,
        data=base64.b64encode(data).decode(),sha256=hashlib.sha256(data).hexdigest())])


def test_append_retry_and_gap_are_idempotent(tmp_path):
    root=tmp_path/'remote'
    def receive(p):return m.receive(p,root=root,status_paths={})
    p=packet();assert receive(p)['offsets']['ssh.log']==len(b'connection refused\n')
    receive(p)
    assert (root/('a'*32)/'ssh.log').read_bytes()==b'connection refused\n'
    assert receive(packet(b'next',999))['offsets']['ssh.log']==19
    receive(packet(b'next',19))
    assert (root/('a'*32)/'ssh.log').read_bytes().endswith(b'\nnext')
    with pytest.raises(ValueError,match='rewrite'):receive(packet(b'changed'))


@pytest.mark.parametrize('bad',['../outside','../ssh.log','private-key','console.log/../../oops'])
def test_log_destination_cannot_escape_allowlist(tmp_path,bad):
    with pytest.raises(ValueError):m.receive(packet(name=bad),root=tmp_path,status_paths={})


def test_checksum_and_symlink_rejected(tmp_path):
    p=packet();p['files'][0]['sha256']='0'*64
    with pytest.raises(ValueError):m.receive(p,root=tmp_path,status_paths={})
    target=tmp_path/('a'*32);outside=tmp_path/'outside';outside.write_text('keep')
    (target/'ssh.log').symlink_to(outside)
    with pytest.raises(OSError):m.receive(packet(),root=tmp_path,status_paths={})
    assert outside.read_text()=='keep'


def test_sync_reconnect_resumes_real_bytes_and_binds_run(tmp_path):
    local=tmp_path/'local'/('a'*32);local.mkdir(parents=True)
    (local/'ssh.log').write_text('first\n');calls=[]
    def run(args,**kwargs):
        request=json.loads(kwargs['input']);calls.append(request)
        response=m.receive(request,root=tmp_path/'remote',status_paths={})
        response['receiver']=dict(run_id='f1_hil_expected',pid=7,start_ticks='8',alive=False,fresh=False,state='stopping')
        return subprocess.CompletedProcess(args,0,json.dumps(response),'')
    d=m.Diagnostics(local,['ssh','host'],'rlt400',run=run)
    d.accept_ready('f1_hil_other');d.sync_once();assert not d.receiver_ended.is_set()
    (local/'ssh.log').write_text('first\nsecond\n')
    d=m.Diagnostics(local,['ssh','host'],'rlt400',run=run)
    d.accept_ready('f1_hil_expected');d.sync_once()
    assert calls[-1]['files'][0]['offset']==6 and d.receiver_ended.is_set()
    assert (tmp_path/'remote'/('a'*32)/'ssh.log').read_text()=='first\nsecond\n'


def test_stop_targets_only_matching_live_process_once(tmp_path):
    import time
    # A harmless child named for the expected module; no robot import or output.
    code="import time; print('READY',flush=True); time.sleep(30)"
    child=subprocess.Popen([sys.executable,'-u','-c',code,'f1dagger'],stdout=subprocess.PIPE,text=True)
    try:
        assert child.stdout.readline().strip()=='READY'
        ticks=Path(f'/proc/{child.pid}/stat').read_text().rsplit(')',1)[1].split()[19]
        identity=dict(pid=child.pid,start_ticks=ticks,run_id='f1_hil_test')
        status=tmp_path/'status.json';status.write_text(json.dumps(dict(identity,updated_unix=time.time(),status=dict(state='episode_ready'),capabilities=['finish_session_v1'])))
        p=packet();p['stop']=dict(identity,run_id='f1_hil_wrong')
        r=m.receive(p,root=tmp_path/'remote',status_paths={'rlt400':str(status)})
        assert 'stop_sent' not in r
        assert not (tmp_path/'finish-request.json').exists()
        p['stop']=identity
        assert m.receive(p,root=tmp_path/'remote',status_paths={'rlt400':str(status)})['stop_sent']==identity
        finish=(tmp_path/'finish-request.json').read_bytes()
        assert json.loads(finish)['run_id']==identity['run_id']
        m.receive(p,root=tmp_path/'remote',status_paths={'rlt400':str(status)})
        assert (tmp_path/'finish-request.json').read_bytes()==finish
        assert child.poll() is None  # Caller writes a request, never kills a PID.
    finally:
        child.terminate();child.wait(timeout=5)


def test_remote_program_imports_without_client_dependencies(tmp_path):
    code=m.remote_program().replace(repr(m.REMOTE_ROOT),repr(str(tmp_path/'remote')),1)
    result=subprocess.run([sys.executable,'-c',code],input=json.dumps(packet())+'\n',text=True,capture_output=True)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['offsets']['ssh.log']==19


def test_failed_network_keeps_local_bytes_and_unacknowledged_offset(tmp_path):
    local=tmp_path/('b'*32);local.mkdir();(local/'sender.log').write_text('disconnect evidence\n')
    def fail(args,**kwargs):raise subprocess.TimeoutExpired(args,8)
    d=m.Diagnostics(local,['ssh','host'],'rlt400',run=fail)
    with pytest.raises(subprocess.TimeoutExpired):d.sync_once()
    assert not (local/'sync-offsets.json').exists()
    assert (local/'sender.log').read_text()=='disconnect evidence\n'


def test_locked_ssh_log_does_not_block_other_logs_or_receiver_status(tmp_path,monkeypatch):
    local=tmp_path/('c'*32);local.mkdir()
    for name in m.FILES:(local/name).write_text('evidence '+name+'\n')
    original=Path.open;locked=True
    def open_file(path,*a,**kw):
        if locked and path==local/'ssh.log' and a==('rb',):raise PermissionError('Windows sharing violation')
        return original(path,*a,**kw)
    monkeypatch.setattr(Path,'open',open_file)
    def run(args,**kw):
        response=m.receive(json.loads(kw['input']),root=tmp_path/'remote',status_paths={})
        response['receiver']=dict(run_id='f1_hil_test',alive=False,fresh=False,state='stopping')
        return subprocess.CompletedProcess(args,0,json.dumps(response),'')
    d=m.Diagnostics(local,['ssh','host'],'rlt400',run=run);d.accept_ready('f1_hil_test');d.sync_once()
    assert d.receiver_ended.is_set() and d.receiver_stopping.is_set()
    remote=tmp_path/'remote'/local.name
    assert (remote/'console.log').read_bytes()==(local/'console.log').read_bytes()
    assert not (remote/'ssh.log').exists()
    assert 'sync_file_deferred' in (local/'launcher.jsonl').read_text()
    locked=False;d.sync_once()
    assert (remote/'ssh.log').read_bytes()==(local/'ssh.log').read_bytes()

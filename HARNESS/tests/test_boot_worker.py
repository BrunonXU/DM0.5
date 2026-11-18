import json
from pathlib import Path
import threading
import queue
from types import SimpleNamespace as N
import pytest
from f1_harness.boot_worker import BootRequests
from f1_harness.boot_client import WarmBootClient


@pytest.fixture
def worker(tmp_path,monkeypatch):
    site=tmp_path/('web-'+'a'*32);site.mkdir()
    (site/'development.local.json').write_text(json.dumps({'task_hash':'b'*64}))
    calls=[]
    def refresh(path,*,session):calls.append(path)
    monkeypatch.setattr('f1_harness.boot_worker.refresh_boot_site',refresh)
    return BootRequests(object(),site_root=tmp_path),dict(site=str(site),task_hash='b'*64,request_id='c'*32),calls


def test_duplicate_boot_request_does_not_rewrite_evidence(worker):
    service,body,calls=worker
    first=service.capture(body);second=service.capture(body)
    assert first==second and first['ok'] and len(calls)==1
    with pytest.raises(ValueError,match='rebound'):
        service.capture(dict(body,task_hash='d'*64))


def test_failed_capture_is_not_silently_reexecuted(worker,monkeypatch):
    service,body,calls=worker
    def fail(path,*,session):calls.append(path);raise ValueError('fresh_feedback_missing')
    monkeypatch.setattr('f1_harness.boot_worker.refresh_boot_site',fail)
    assert not service.capture(body)['ok']
    assert 'fresh_feedback_missing' in service.capture(body)['error'] and len(calls)==1


@pytest.mark.parametrize('field,value',[('task_hash','d'*64),('site','/tmp/unregistered'),('request_id','invalid')])
def test_request_binding_rejects_before_capture(worker,field,value):
    service,body,calls=worker
    with pytest.raises(ValueError):service.capture(dict(body,**{field:value}))
    assert not calls


def client(tmp_path,reply=None):
    c=WarmBootClient({},tmp_path);c.ready.set();sent=[]
    class Input:
        def write(self,line):
            request=json.loads(line);sent.append(request)
            if reply is not None:c.responses.put(reply(request))
        def flush(self):pass
        def close(self):pass
    c.process=N(stdin=Input(),poll=lambda:None,wait=lambda **kw:None)
    return c,sent


def test_client_uses_warm_result_bound_to_current_request(tmp_path):
    c,sent=client(tmp_path,lambda r:dict(r,ok=True))
    assert c.capture('/site','a'*64,threading.Event()) is True
    assert len(sent)==1


def test_cold_fallback_only_before_dispatch(tmp_path):
    c=WarmBootClient({},tmp_path)
    assert c.capture('/site','a'*64,threading.Event()) is False


@pytest.mark.parametrize('fault',['wrong_id','rejected','connection_lost','timeout'])
def test_failed_dispatched_request_never_returns_cold_fallback(tmp_path,monkeypatch,fault):
    def reply(r):
        return dict(r,request_id='other' if fault=='wrong_id' else r['request_id'],ok=False,error='geometry_changed')
    c,sent=client(tmp_path,reply if fault in ('wrong_id','rejected') else None)
    if fault in ('connection_lost','timeout'):
        clock=[0.]
        monkeypatch.setattr('f1_harness.boot_client.time.monotonic',lambda:clock[0])
        def empty(**kw):
            if fault=='connection_lost':c.ended.set()
            clock[0]+=20
            raise queue.Empty
        c.responses.get=empty
    with pytest.raises((RuntimeError,TimeoutError)):
        c.capture('/site','a'*64,threading.Event())
    assert len(sent)==1


@pytest.mark.parametrize('route',['warm','cold','unknown'])
def test_workspace_boot_path_never_replays_unknown_warm_request(tmp_path,monkeypatch,route):
    from f1_harness.web_process import WorkspaceProcesses
    process=WorkspaceProcesses.__new__(WorkspaceProcesses)
    process.run=tmp_path/'web-run';(process.run/'site').mkdir(parents=True)
    process.config=dict(remote_bundle='/bundle',known_hosts='/known',identity='/identity',port=22,host='robot')
    process.instructions={'remote_services_run':'/run'};process.ssh=['ssh','robot']
    process.helper=N(REMOTE_GUARD='guard');process.expected_task_hash='a'*64
    spawned=[];downloads=[]
    def capture(*args):
        if route=='unknown':raise TimeoutError('boot_worker_result_unknown')
        return route=='warm'
    process.boot_client=N(capture=capture)
    process.spawn=lambda row:(spawned.append(row) or N(poll=lambda:0,returncode=0))
    process.check=lambda:None
    def run(argv,**kwargs):
        downloads.append(argv)
        (process.run/'site/boot-recheck.json').write_text('{"accepted":true}')
    monkeypatch.setattr('f1_harness.web_process.subprocess.run',run)
    if route=='unknown':
        with pytest.raises(TimeoutError,match='result_unknown'):
            process.prepare_boot_recheck(threading.Event())
        assert not spawned and len(downloads)==1
        assert downloads[0][-2].endswith('boot-recheck-evidence.json')
    else:
        assert process.prepare_boot_recheck(threading.Event())=={'accepted':True}
        assert len(spawned)==(route=='cold')
        assert json.loads((process.run/'boot-route.json').read_text())['warm']==(route=='warm')

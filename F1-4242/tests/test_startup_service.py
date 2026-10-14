import sys,hashlib
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import startup_service as mod

def make(tmp_path,monkeypatch,processes=()):
    script=tmp_path/'start_all.sh';script.write_text('#!/bin/bash\necho fixture\n');monkeypatch.setattr(mod,'SCRIPT',script);calls=[]
    def run(args,**kwargs):calls.append(args);return SimpleNamespace(stdout='inactive\n',stderr='',returncode=0)
    obj=mod.StartupService({'startup_script_sha256':hashlib.sha256(script.read_bytes()).hexdigest()},tmp_path,run=run,processes=lambda:list(processes))
    return obj,calls,script

def test_read_only_state_never_launches_and_duplicates_are_blocked(tmp_path,monkeypatch):
    obj,calls,_=make(tmp_path,monkeypatch,[12]);assert not obj.state()['can_start']
    with pytest.raises(ValueError,match='重复'):obj.start()
    assert all(c[0]=='systemctl' for c in calls)

def test_explicit_start_is_fixed_path_not_shell_text(tmp_path,monkeypatch):
    obj,calls,script=make(tmp_path,monkeypatch);assert obj.start()['accepted'];cmd=calls[-1]
    assert cmd[0]=='systemd-run' and cmd[-2:]==['/bin/bash',str(script)]
    assert '--property=RemainAfterExit=yes' in cmd and not any(x=='-c' for x in cmd)

def test_changed_upstream_script_requires_review(tmp_path,monkeypatch):
    obj,calls,script=make(tmp_path,monkeypatch);script.write_text('changed')
    with pytest.raises(ValueError,match='变化'):obj.start()
    assert all(c[0]!='systemd-run' for c in calls)

from test_data_auth import http_app

def test_http_start_requires_login_origin_confirmation_and_no_lease(http_app,monkeypatch):
    app,request=http_app;calls=[]
    monkeypatch.setattr(app.startup,'state',lambda:{'can_start':True})
    monkeypatch.setattr(app.startup,'start',lambda:(calls.append('start') or {'accepted':True}))
    _,_,cookie=request('/api/session')
    assert request('/api/startup/state',cookie=cookie)[0]==200 and calls==[]
    assert request('/api/startup/start',{'confirm':True},cookie)[0]==401
    request('/api/login',{'control_code':'fixture-control-code'},cookie)
    assert request('/api/startup/start',{'confirm':True},cookie,origin=False)[0]==403
    assert request('/api/startup/start',{},cookie)[0]==409
    assert request('/api/startup/start',{'confirm':True},cookie)[0]==200 and calls==['start']
    request('/api/control/acquire',{'control_code':'fixture-control-code'},cookie)
    assert request('/api/startup/start',{'confirm':True},cookie)[0]==409 and calls==['start']

def test_waist_preview_is_read_only_and_rejects_bad_input(http_app,monkeypatch):
    app,request=http_app;calls=[]
    monkeypatch.setattr(app.adapter,'plan',lambda spec:(calls.append(spec) or {'targets':{},'motion_s':1}),raising=False)
    assert request('/api/waist/preview?delta_m=0.02')[0]==200
    assert calls==[{'kind':'waist_height','delta_m':.02}]
    assert request('/api/waist/preview?delta_m=bad')[0]==400
    assert app.lease.fd is None and not app.actions.busy()

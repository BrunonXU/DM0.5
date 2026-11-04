"""Read-only archive compatibility and session-bound graceful finish."""
import json
import time
import pytest
from test_hil_upgrade import fixture_run
from hil_data import HILData, episode_id, find_run
from hil_service import HILService
from f1dagger.lifecycle import finish_requested, publish_completion, session_root


def make_services(tmp_path):
    runtime=tmp_path/'runtime';runtime.mkdir()
    (tmp_path/'hil.json').write_text(json.dumps(dict(schema='f1_hil_panel_v1',runtime=str(runtime),data_root=str(tmp_path/'dagger_data'),launcher='/tmp/start',ssh_host='user@robot')))
    now=[time.time()];process=dict(pid=123,start_ticks='456',started_unix=now[0]-10)
    hil=HILService(tmp_path,clock=lambda:now[0],process_reader=lambda:[process])
    return runtime,hil,now,process


def snapshot(runtime,now,process,run_id='session'):
    s=dict(schema='f1_hil_status_v1',**process,updated_unix=now[0],run_id=run_id,binding={},status=dict(state='hil_latched',input_fresh=True),capabilities=['finish_session_v1'])
    (runtime/'status.json').write_text(json.dumps(s));return {k:s[k] for k in ('run_id','pid','start_ticks')}


def test_finish_identity_expiry_and_completion(tmp_path):
    runtime,hil,now,process=make_services(tmp_path);key=snapshot(runtime,now,process)
    assert hil.state()['can_finish']
    with pytest.raises(ValueError):hil.finish({'session_key':dict(key,run_id='old')})
    result=hil.finish({'session_key':key});assert not result['saved']
    request=runtime/'finish-request.json'
    assert finish_requested(request,key,now[0])
    assert not finish_requested(request,dict(key,start_ticks='reused-pid'),now[0])
    assert not finish_requested(request,key,now[0]+11)
    now[0]+=3
    with pytest.raises(ValueError):hil.finish({'session_key':key})


def test_stop_unsupported_old_receiver(tmp_path):
    runtime,hil,now,process=make_services(tmp_path);key=snapshot(runtime,now,process)
    s=json.loads((runtime/'status.json').read_text());s.pop('capabilities');(runtime/'status.json').write_text(json.dumps(s))
    with pytest.raises(ValueError):hil.finish({'session_key':key})
    assert not (runtime/'finish-request.json').exists()


def test_legacy_and_new_storage_replay_integrity(tmp_path):
    runtime,hil,_,_=make_services(tmp_path)
    run=fixture_run(runtime,'demonstration');data=HILData(hil);eid=episode_id(run)
    row=data.catalog()[0];assert row['source']=='hil_demonstration' and row['recording_status']=='committed'
    assert not row['training_ready']
    timeline=data.timeline(eid);assert timeline['frames']==15
    frames=[data.frame(eid,c,.12) for c in timeline['cameras']]
    assert len({x[1]['observation_id'] for x in frames})==1
    assert all(x[0].startswith(b'\xff\xd8') for x in frames)
    new=tmp_path/'dagger_data/demonstration'/run.name;new.parent.mkdir(parents=True);run.rename(new)
    assert data.catalog()[0]['id']==eid
    assert find_run(hil,run.name)==new
    assert data.frame(eid,'head',.12)[0]==frames[0][0]
    p=new/'payload/head.pack'
    with p.open('r+b') as f:f.write(b'\x00')
    with pytest.raises(ValueError,match='hash'):data.frame(eid,'head',0)


def test_incomplete_and_symlink_never_saved(tmp_path):
    runtime,hil,_,_=make_services(tmp_path);run=fixture_run(runtime,'demonstration');data=HILData(hil)
    p=run/'payload/manifest.json';m=json.loads(p.read_text());m['complete']=False;p.write_text(json.dumps(m))
    assert data.catalog()[0]['recording_status']=='recovery'
    with pytest.raises(ValueError):data.timeline(episode_id(run))
    c=publish_completion(runtime,run,dict(purpose='demonstration',recording_mode='hil-record'),'saved')
    assert c['phase']=='failed'
    run.rename(runtime/'outside');(runtime/'raw'/'alias').symlink_to(runtime/'outside')
    assert not data.catalog()


def test_roots_and_completed_save(tmp_path):
    runtime,hil,_,_=make_services(tmp_path);run=fixture_run(runtime,'demonstration')
    assert session_root(runtime,tmp_path/'dagger_data','demonstration')==tmp_path/'dagger_data/demonstration'
    assert session_root(runtime,tmp_path/'dagger_data','teleop_play')==runtime/'raw'
    c=publish_completion(runtime,run,dict(purpose='demonstration',recording_mode='hil-record'),'saved')
    assert c['phase']=='saved' and c['observations']==15 and not c['training_ready']
    link=tmp_path/'link';link.symlink_to(runtime,target_is_directory=True)
    with pytest.raises(ValueError):session_root(runtime,link,'demonstration')

from test_data_auth import http_app

def test_finish_requires_auth_same_origin_and_never_acquires_motion(http_app,monkeypatch):
    app,request=http_app
    _,_,cookie=request('/api/session')
    called=[]
    monkeypatch.setattr(app.hil,'finish',lambda body:called.append(body) or {'requested':True,'saved':False,'run_id':'test'})
    assert request('/api/hil/finish',{'session_key':{}},cookie,origin=False)[0]==403
    assert request('/api/login',{},cookie)[0]==200
    assert request('/api/hil/finish',{'session_key':{}},cookie,origin=False)[0]==403
    assert not called
    assert request('/api/hil/finish',{'session_key':{}},cookie)[0]==200
    assert len(called)==1 and app.lease.fd is None and app.actions.latest() is None

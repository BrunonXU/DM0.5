"""Regression for state/review/report changes; no robot or real service starts."""
import concurrent.futures
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_data_auth import episode, http_app
from data_service import DataService, ReviewConflict
from replay_reports import record_latest, recent
from test_startup_service import make


def test_review_compare_and_swap_two_reviewers(tmp_path):
    episode(tmp_path/'raw')
    store=DataService(tmp_path/'raw', tmp_path/'runtime', tmp_path/'web')
    row=store.catalog()['episodes'][0]
    def save(note):
        try:
            return store.save_review(row['id'],'approved',note,note,row['source_signature'],row['review']['revision'])
        except ReviewConflict as exc:
            return exc
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results=list(pool.map(save,['A','B']))
    assert sum(isinstance(r,dict) for r in results)==1
    conflict=next(r for r in results if isinstance(r,ReviewConflict))
    assert conflict.code=='REVIEW_CONFLICT'
    assert conflict.review['revision']!='none'
    assert store.episode(row['id'],refresh=True)['review']['notes'] in ('A','B')


def test_review_legacy_revision_and_missing_version(tmp_path):
    episode(tmp_path/'raw');store=DataService(tmp_path/'raw',tmp_path/'runtime',tmp_path/'web')
    row=store.catalog()['episodes'][0];store.review_root.mkdir(parents=True)
    sidecar=store.review_root/(row['id']+'.json')
    sidecar.write_text(json.dumps(dict(status='approved',notes='old',source_signature=row['source_signature'])))
    before=sidecar.read_bytes();review=store.episode(row['id'],refresh=True)['review']
    assert len(review['revision'])==64 and sidecar.read_bytes()==before
    with pytest.raises(ReviewConflict) as err:store.save_review(row['id'],'rejected','','B',row['source_signature'])
    assert err.value.code=='REVIEW_REFRESH_REQUIRED'
    assert sidecar.read_bytes()==before


def test_http_conflict_is_structured_and_never_acquires_lease(http_app):
    app,request=http_app
    _,catalog,cookie=request('/api/data');row=catalog['episodes'][0]
    request('/api/login',{},cookie)
    body=dict(episode_id=row['id'],status='approved',expected_source_signature=row['source_signature'],expected_review_revision=row['review']['revision'])
    assert request('/api/data/reviews',body,cookie)[0]==200
    status,result,_=request('/api/data/reviews',body,cookie)
    assert status==409 and result['code']=='REVIEW_CONFLICT' and result['review']['status']=='approved'
    body['expected_source_signature']='old'
    assert request('/api/data/reviews',body,cookie)[1]['code']=='SOURCE_CHANGED'
    assert app.lease.fd is None and app.actions.latest() is None


def test_recent_report_survives_reload_and_rejects_symlinks(tmp_path):
    eid='e'*24;rid='a'*32;root=tmp_path/'reports';root.mkdir()
    path=root/(rid+'.json');path.write_text('{}')
    assert recent(root,eid)==[]
    record_latest(path,dict(episode_id=eid,report_id=rid,source_signature='source',created_at=1),dict(status='cancelled'))
    result=recent(root,eid)[0]
    assert result['report_id']==rid and result['source_signature']=='source' and result['status']=='cancelled'
    index=root/'latest'/(eid+'.json');index.unlink();index.symlink_to(path)
    with pytest.raises(ValueError):recent(root,eid)
    with pytest.raises(ValueError):recent(root,'../outside')


def test_start_is_nonblocking_and_separates_script_completion(tmp_path,monkeypatch):
    obj,calls,_=make(tmp_path,monkeypatch)
    assert obj.start()['accepted'] and '--no-block' in calls[-1]
    def run(args,**kwargs):
        return SimpleNamespace(stdout='ActiveState=activating\nSubState=start\nResult=success\nExecMainStatus=0\n',returncode=0)
    obj.run=run
    assert obj.state(force=True)['stage']=='starting' and not obj.state()['can_start']
    obj.run=lambda *a,**k:SimpleNamespace(stdout='ActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0\n',returncode=0)
    result=obj.state(force=True)
    assert result['stage']=='completed' and not result['can_start']
    obj.run=lambda *a,**k:SimpleNamespace(stdout='ActiveState=failed\nSubState=failed\nResult=exit-code\nExecMainStatus=1\n',returncode=0)
    assert obj.state(force=True)['stage']=='failed'


def test_saved_hil_take_belongs_to_its_process_not_its_take_id(tmp_path):
    import time
    from hil_service import HILService
    from hil_data import episode_id
    runtime=tmp_path/'runtime';runtime.mkdir()
    (tmp_path/'hil.json').write_text(json.dumps(dict(schema='f1_hil_panel_v1',runtime=str(runtime),launcher='/tmp/start',ssh_host='user@robot')))
    now=time.time()
    process=dict(pid=123,start_ticks='456',started_unix=now-10)
    hil=HILService(tmp_path,clock=lambda:now,process_reader=lambda:[process])
    status=dict(schema='f1_hil_status_v1',**process,updated_unix=now,run_id='process-session',binding={},status=dict(state='hil_latched',input_fresh=True),capabilities=['finish_session_v1'])
    (runtime/'status.json').write_text(json.dumps(status))
    run=runtime/'raw'/'take-one';(run/'journal').mkdir(parents=True);(run/'payload').mkdir()
    (run/'journal/manifest.json').write_text(json.dumps(dict(complete=True,counts={'observations':0},binding={'purpose':'demonstration'})))
    (run/'payload/manifest.json').write_text(json.dumps(dict(complete=True,count=0)))
    manifest = run/'journal/manifest.json'
    content = json.loads(manifest.read_text())
    content['binding']['process_run_id'] = 'process-session'
    manifest.write_text(json.dumps(content))
    completion = dict(schema='f1_session_completion_v1',phase='saved',run_id=run.name)
    (runtime/'last-session.json').write_text(json.dumps(completion))
    state = hil.state()
    assert state['run_id'] != run.name
    assert state['completion']['process_run_id'] == 'process-session'
    assert state['completion']['data_episode_id'] == episode_id(run)
    content['binding']['process_run_id'] = 'another-process'
    manifest.write_text(json.dumps(content));hil.cached_at=0
    assert 'completion' not in hil.state()


def test_recorder_only_links_complete_saved_capture(tmp_path):
    from recording_service import RecordingService
    from recording.unified.catalog import identity
    obj = object.__new__(RecordingService)
    output = tmp_path/'raw';run=output/'capture';run.mkdir(parents=True)
    (run/'capture.json').write_text(json.dumps(dict(committed=True,complete=True)))
    saved = dict(path=str(run),id='recorder-session-id')
    assert obj.saved_identity(saved,output)['data_episode_id']==identity(run)
    (run/'capture.json').write_text(json.dumps(dict(committed=False,complete=False)))
    assert 'data_episode_id' not in obj.saved_identity(saved,output)
    assert 'data_episode_id' not in obj.saved_identity(dict(saved,kind='recovery'),output)


def test_start_submission_timeout_is_uncertain_not_failed(tmp_path, monkeypatch):
    import subprocess
    obj,calls,_=make(tmp_path,monkeypatch)
    previous=obj.run
    def run(args,**kw):
        if args[0]=='systemd-run':raise subprocess.TimeoutExpired(args,5)
        return previous(args,**kw)
    obj.run=run
    result=obj.start()
    assert result['accepted'] is None and '待核对' in result['message']

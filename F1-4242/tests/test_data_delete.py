"""Only temporary fixtures are removed; never require a live robot."""
import fcntl
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import threading
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from data_delete import DataDeletion
from data_service import DataService
from core import EventLog
from test_data_auth import http_app


def fixture_app(tmp_path):
    root=tmp_path/'raw';root.mkdir()
    runtime=tmp_path/'runtime';runtime.mkdir()
    proc=tmp_path/'proc';proc.mkdir()
    for name in ('one','two'):
        p=root/name;p.mkdir();(p/'data.mcap').write_bytes(b'raw original')
    data=DataService(root,runtime,tmp_path/'web',proc_root=proc)
    app=SimpleNamespace(runtime=runtime,data=data,op_lock=threading.RLock(),
        recording=SimpleNamespace(lock=threading.Lock()),
        hil=SimpleNamespace(state=lambda:dict(active=False)),
        hil_media=SimpleNamespace(lock=threading.RLock()),
        hil_data=SimpleNamespace(index_cache=None),capture_data=SimpleNamespace(cache=None),
        lease=SimpleNamespace(path=tmp_path/'control.lock'),actions=SimpleNamespace(busy=lambda:False),
        events=EventLog(runtime/'events.jsonl'),health_at=1)
    return app


def body(app):
    return dict(confirm=True,episodes=[dict(id=r['id'],source_signature=r['source_signature']) for r in app.data.catalog(True)['episodes']])


def test_batch_removes_only_selected_sources_and_derived(tmp_path):
    app=fixture_app(tmp_path);b=body(app);b['episodes']=b['episodes'][:1];eid=b['episodes'][0]['id']
    raw=Path(app.data.episode(eid)['path'])
    preview=app.data.preview_root/eid;preview.mkdir(parents=True);(preview/'head.mp4').write_bytes(b'video')
    review=app.data.review_root/(eid+'.json');review.parent.mkdir();review.write_text('{}')
    exports=tmp_path/'datasets';exports.mkdir();(exports/'keep').write_text('independent export')
    result=DataDeletion(app,capture_active=lambda:False).delete(b,'tester')
    assert result['complete'] and len(result['results'])==1
    assert not raw.exists() and not preview.exists() and not review.exists()
    assert len(app.data.catalog()['episodes'])==1 and (exports/'keep').exists()
    audit=json.loads(next((app.runtime/'data_deletions').glob('*.json')).read_text())
    assert audit['state']=='complete' and app.health_at==0


@pytest.mark.parametrize('fault',['signature','missing','special','active','lock','duplicate','confirmation','root'])
def test_invalid_selection_never_partially_deletes(tmp_path,fault):
    app=fixture_app(tmp_path);b=body(app);store=DataDeletion(app,capture_active=lambda:fault=='active')
    lock=None
    if fault=='signature':b['episodes'][1]['source_signature']='stale'
    if fault=='missing':b['episodes'][1]['id']='0'*24
    if fault=='duplicate':b['episodes'][1]=b['episodes'][0]
    if fault=='confirmation':b['confirm']=False
    if fault=='special':__import__('os').mkfifo(app.data.root/'two'/'pipe')
    if fault=='root':app.data._episode=lambda directory:dict(id='a'*24,path=str(app.data.root),source='lxl',source_signature='abc',size_bytes=1,relative_path='.');b['episodes']=[dict(id='a'*24,source_signature='abc')]
    if fault=='lock':
        lock=app.lease.path.open('a+');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
        with pytest.raises(ValueError):store.delete(b,'tester')
        assert (app.data.root/'one/data.mcap').exists() and (app.data.root/'two/data.mcap').exists()
    finally:
        if lock:lock.close()


def test_cleanup_failure_is_audited_not_claimed_as_deleted(tmp_path,monkeypatch):
    app=fixture_app(tmp_path);b=body(app);b['episodes']=b['episodes'][:1]
    import data_delete
    monkeypatch.setattr(data_delete.shutil,'rmtree',lambda *a,**kw:(_ for _ in ()).throw(OSError('disk IO fixture')))
    result=DataDeletion(app,capture_active=lambda:False).delete(b,'tester')
    assert not result['complete'] and result['results'][0]['state']=='staged'
    assert Path(result['results'][0]['staged']).exists()
    assert len(app.data.catalog(True)['episodes'])==1


def test_hil_jobs_removed_and_leased_jobs_block(tmp_path):
    app=fixture_app(tmp_path)
    root=tmp_path/'hil';raw=root/'raw'/'f1_hil_fixture';raw.mkdir(parents=True)
    (raw/'data.bin').write_bytes(b'camera')
    config=tmp_path/'hil.json';config.write_text(json.dumps(dict(runtime=str(root))))
    app.hil.config_path=config;app.hil_media.root=lambda:root
    row=dict(id='a'*24,path=str(raw),source='hil_demonstration',run_id=raw.name,name=raw.name,source_signature='sha',size_bytes=6,relative_path=str(raw))
    app.data.extra_catalog=lambda:[row] if raw.exists() else []
    key='b'*64;p=root/'video-jobs'/(key+'.json');p.parent.mkdir();job=dict(id=key,run_id=raw.name,lease_until=9999999999);p.write_text(json.dumps(job))
    b=dict(confirm=True,episodes=[dict(id=row['id'],source_signature='sha')])
    store=DataDeletion(app,capture_active=lambda:False)
    with pytest.raises(ValueError,match='视频正在'):store.delete(b,'tester')
    assert raw.exists()
    job['lease_until']=0;p.write_text(json.dumps(job))
    video=root/'videos'/key;video.mkdir(parents=True);(video/'clip.mp4').write_bytes(b'video')
    assert store.delete(b,'tester')['complete']
    assert not p.exists() and not video.exists() and not raw.exists()


def test_delete_http_requires_auth_origin_and_source_signature(http_app):
    app,request=http_app;app.data_delete.capture_active=lambda:False
    _,catalog,cookie=request('/api/data');row=catalog['episodes'][0]
    b=dict(confirm=True,episodes=[dict(id=row['id'],source_signature=row['source_signature'])])
    assert request('/api/data/delete',b,cookie,origin=False)[0]==403
    assert request('/api/login',{},cookie)[0]==200
    assert request('/api/data/delete',b,cookie,origin=False)[0]==403
    assert request('/api/data/delete',dict(b,confirm=False),cookie)[0]==409
    assert request('/api/data/delete',b,cookie)[1]['complete']
    assert not Path(row['path']).exists() and app.lease.fd is None and app.actions.latest() is None
    assert request('/api/data/delete',b,cookie)[0]==409


def test_internal_links_removed_without_following_targets(tmp_path):
    app=fixture_app(tmp_path)
    outside=tmp_path/'outside';outside.mkdir();(outside/'keep.txt').write_bytes(b'untouched')
    take=app.data.root/'one'
    (take/'linked-file').symlink_to(outside/'keep.txt')
    (take/'linked-directory').symlink_to(outside,target_is_directory=True)
    (take/'broken-link').symlink_to(tmp_path/'missing')
    (take/'cycle').symlink_to(take,target_is_directory=True)
    b=body(app);eid=next(r['id'] for r in app.data.catalog()['episodes'] if r['name']=='one')
    b['episodes']=[r for r in b['episodes'] if r['id']==eid]
    result=DataDeletion(app,capture_active=lambda:False).delete(b,'tester')
    assert result['complete'] and not take.exists()
    assert (outside/'keep.txt').read_bytes()==b'untouched'
    assert (app.data.root/'two/data.mcap').exists()


def test_source_root_link_still_refused(tmp_path):
    from data_delete import inventory
    real=tmp_path/'real';real.mkdir();(real/'keep').write_bytes(b'keep')
    link=tmp_path/'linked';link.symlink_to(real,target_is_directory=True)
    with pytest.raises(ValueError):inventory(link)
    assert (real/'keep').read_bytes()==b'keep'

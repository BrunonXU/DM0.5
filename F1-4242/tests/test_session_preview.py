"""Real encode, atomic publication, Range reads, isolation and source immutability."""
import importlib.util
import json
import subprocess
from pathlib import Path
import threading
import urllib.request
import pytest
from test_hil_upgrade import fixture_run
from hil_media import digest, PREVIEW_VERSION
from server import App, Server
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('session_worker',ROOT/'scripts/session_preview_worker.py');worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)

@pytest.mark.parametrize('failure',[None,'upload','unconfirmed','lost_reply'])
@pytest.mark.parametrize('tilde_runtime',[False,True])
def test_session_video_roundtrip(tmp_path,monkeypatch,tilde_runtime,failure):
    monkeypatch.setenv('HOME',str(tmp_path))
    runtime=tmp_path/'console';runtime.mkdir();root=tmp_path/'dagger';run=fixture_run(root,'demonstration',camera_sizes={'head':(128,72),'left_wrist':(64,48),'right_wrist':(64,48)})
    (runtime/'hil.json').write_text(json.dumps(dict(schema='f1_hil_panel_v1',runtime='~/dagger' if tilde_runtime else str(root),launcher='/tmp/start',ssh_host='user@robot')))
    profile=json.loads((ROOT/'configs/robot.example.json').read_text());app=App(profile,runtime,tmp_path/'lock',offline=True)
    http=Server(('127.0.0.1',0),app,[]);url='http://127.0.0.1:'+str(http.server_port);http.allowed.add(url.split('//')[1]);thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    before={str(p):digest(p) for p in run.rglob('*') if p.is_file()}
    try:
        client=worker.Client(dict(robot_url=url,access_token_file=str(app.token_path)))
        job=client.post('/api/hil/media/claim',dict(kind='session_preview'))['job'];assert job['renderer']==PREVIEW_VERSION
        assert client.post('/api/hil/media/claim',dict(kind='session_preview'))['job'] is None
        assert app.hil_data.catalog()[0]['preview_status']=='transferring'
        busy=app.hil_media.busy;app.hil_media.busy=lambda:True
        with pytest.raises(ValueError,match='paused'):client.op('source',job,name='payload/head.pack',offset=0,count=100)
        app.hil_media.busy=busy
        with pytest.raises(ValueError):client.op('upload',job,name='../bad',offset=0,data='')
        with pytest.raises(ValueError):client.op('publish_preview',job,files={})
        cached=tmp_path/'server-cache'/job['id']/'journal/manifest.json';cached.parent.mkdir(parents=True);cached.write_bytes((run/'journal/manifest.json').read_bytes()[:19])
        if failure:
            transfer=client.transfer;upload=client.upload
            def fail_transfer(op,j,**kw):
                if op=='publish_preview':
                    if failure=='unconfirmed':return {'ready':False}
                    result=transfer(op,j,**kw)
                    if failure=='lost_reply':raise worker.URLError('publication response lost')
                    return result
                return transfer(op,j,**kw)
            client.transfer=fail_transfer
            if failure=='upload':
                def fail_upload(*args):raise OSError('upload interrupted')
                client.upload=fail_upload
            with pytest.raises((ValueError,OSError)):
                worker.process(client,job,tmp_path/'server-cache')
            assert (tmp_path/'server-cache'/job['id']).is_dir()
            assert client.published(job)==(failure=='lost_reply')
            worker.retry_publications(client,tmp_path/'server-cache')
            if failure=='lost_reply':assert not (tmp_path/'server-cache'/job['id']).exists()
            else:assert (tmp_path/'server-cache'/job['id']).is_dir()
            client.transfer=transfer;client.upload=upload
        if failure!='lost_reply':worker.process(client,job,tmp_path/'server-cache')
        assert not (tmp_path/'server-cache'/job['id']).exists()
        receipt=json.loads(worker.receipt_path(tmp_path/'server-cache',job).read_text());assert receipt['published'] and receipt['cache_removed']
        row=app.hil_data.catalog()[0];assert len(row['videos'])==3 and not row['training_ready'] and row['preview_status']=='ready'
        folder=runtime/'data_previews'/job['episode_id'];timeline=json.loads((folder/'timeline.json').read_text());assert timeline['fps']==30 and len(timeline['frames'])>=14
        assert timeline['encoding']==worker.ENCODING and timeline['video_is_original'] is False
        for camera,size in {'head':(128,72),'left':(64,48),'right':(64,48)}.items():
            path=folder/(camera+'.mp4')
            info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','stream=width,height','-of','json',str(path)]))['streams'][0]
            assert (info['width'],info['height'])==size
            # A fresh decoded frame fills its native canvas, including its edges.
            rgb=subprocess.check_output(['ffmpeg','-v','error','-i',str(path),'-vf','select=eq(n\\,2)','-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1'])
            assert len(rgb)==size[0]*size[1]*3
            assert all(abs(rgb[i]-expected)<8 for i,expected in enumerate((40,120,200)))
            assert all(abs(rgb[-3+i]-expected)<8 for i,expected in enumerate((40,120,200)))
        # Identical JPEG content in fresh callbacks is not a held source frame.
        seen=set()
        for f in timeline['frames']:
            cam=f['cameras']['head'];identity=(cam['frame_id'],cam['received_ns'])
            if identity not in seen:assert cam['repeated'] is False
            seen.add(identity)
        analytics=json.loads((folder/'analytics.json').read_text());assert all(v is None for r in analytics['action'] for v in r) # no publisher in fixture
        assert any(v is not None for r in analytics['state'] for v in r)
        app.data.catalog(refresh=True)  # failure probe cached the pre-publication catalogue
        for v in row['videos']:
            request=urllib.request.Request(url+v['url'],headers={'Range':'bytes=0-15'})
            with urllib.request.urlopen(request) as response:assert response.status==206 and response.read()[4:8]==b'ftyp'
        assert before=={str(p):digest(p) for p in run.rglob('*') if p.is_file()}
        app.hil_media.scan_at=0;app.hil_media.scan();assert client.post('/api/hil/media/claim',dict(kind='session_preview'))['job'] is None
        # Altered sources must not keep serving the previous derived cache.
        p=run/'journal/events.jsonl';p.write_text(p.read_text()+'\n')
        assert not app.hil_data.catalog()[0]['videos']
    finally:http.shutdown();http.server_close();app.close();thread.join(2)


def test_cleanup_confined_and_receipt_required(tmp_path):
    cache=tmp_path/'cache';cache.mkdir();source='a'*64
    job=dict(source_sha256=source,id=worker.hashlib.sha256((source+worker.VERSION).encode()).hexdigest(),episode_id='b'*24)
    outside=tmp_path/'original';outside.mkdir();(outside/'keep.jpg').write_bytes(b'original')
    with pytest.raises(ValueError):worker.cache_job_root(cache,{**job,'id':'../original'})
    root=cache/job['id'];root.symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError):worker.finish_cache(cache,job,{})
    assert (outside/'keep.jpg').read_bytes()==b'original';root.unlink();root.mkdir()
    (root/'part.pack').write_bytes(b'partial')
    with pytest.raises(FileNotFoundError):worker.cleanup_published_job(cache,job)
    assert root.exists()
    receipt=worker.receipt_path(cache,job)
    worker.save_receipt(receipt,dict(job,published=False,renderer=worker.VERSION))
    with pytest.raises(ValueError):worker.cleanup_published_job(cache,job)
    assert root.exists()
    # Restart completes a previously confirmed cleanup, without following nested links.
    (root/'outside').symlink_to(outside,target_is_directory=True)
    worker.save_receipt(receipt,dict(job,published=True,renderer=worker.VERSION,cache_removed=False))
    worker.retry_cleanup(cache)
    assert not root.exists() and (outside/'keep.jpg').read_bytes()==b'original'
    assert json.loads(receipt.read_text())['cache_removed']

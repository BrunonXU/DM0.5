import hashlib,http.client,json,sys,threading,time
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from recording_service import RecordingService,confined_file
from preview_service import byte_range
from server import App,Server

@pytest.mark.parametrize('header,want',[(None,(0,9,False)),('bytes=2-5',(2,5,True)),('bytes=8-',(8,9,True)),('bytes=-3',(7,9,True)),('bytes=0-99',(0,9,True))])
def test_ranges(header,want):assert byte_range(header,10)==want
@pytest.mark.parametrize('header',['bytes=12-20','bytes=4-2','bytes=0-1,4-5','bytes=-0','bytes=-','other=0-1'])
def test_invalid_ranges(header):
    with pytest.raises(ValueError):byte_range(header,10)

def test_status_does_not_start_worker_and_rejects_stale_heartbeat(tmp_path,monkeypatch):
    runtime=tmp_path/'runtime';(runtime/'recording-venv/bin').mkdir(parents=True);(runtime/'recording-venv/bin/python').touch()
    s=RecordingService(runtime,tmp_path);assert s.state()['mode']=='not_started';assert s.child is None
    s.owned.mkdir();(s.owned/'control.sock').touch()
    monkeypatch.setattr(s,'_alive',lambda x:True)
    (s.owned/'status.json').write_text(json.dumps({'mode':'recording','pid':123,'checked_unix':time.time()-10,'ready':True}))
    assert s.state()['mode']=='error' and not s.state()['ready']
    (s.owned/'status.json').write_text(json.dumps({'mode':'recording','pid':123,'checked_unix':float('nan'),'ready':True}))
    assert s.state()['mode']=='error' and not s.state()['ready']
    monkeypatch.setattr(s,'_alive',lambda x:False)
    assert s.state()['mode']=='not_started'

def test_first_start_only_prepares_subscriptions(tmp_path,monkeypatch):
    s=RecordingService(tmp_path,tmp_path)
    monkeypatch.setattr(s,'state',lambda:{'mode':'not_started'})
    monkeypatch.setattr(s,'ensure_worker',lambda:{'mode':'idle','ready':False})
    assert s.command('start')['mode']=='idle'  # No socket: would fail if actual start was attempted.

def test_media_is_confined_and_sources_do_not_need_robot_control(tmp_path):
    profile=json.loads((Path(__file__).resolve().parents[1]/'configs/f1-09.json').read_text());profile['data_path']=str(tmp_path/'raw');(tmp_path/'raw').mkdir()
    app=App(profile,tmp_path/'runtime',tmp_path/'control.lock',offline=True)
    try:
        output=app.recording.output;output.mkdir(parents=True)
        (output/'video.mp4').write_bytes(b'0123456789');(output/'meta.json').write_text('{}');(output/'data.parquet').write_bytes(b'test-file-not-real-parquet')
        eid='a'*24;app.recording.owned.mkdir()
        row={'id':eid,'name':'fixture','source':'lerobot','metadata_sha256':'fixture','relative_path':'data.parquet','files':[{'relative_path':n} for n in ('video.mp4','meta.json','data.parquet')],'videos':[{'camera':'head','relative_path':'video.mp4'}],'size_bytes':39}
        (app.recording.owned/'catalog.json').write_text(json.dumps({'episodes':[row]}))
        server=Server(('127.0.0.1',0),app,[]);server.allowed={f'127.0.0.1:{server.server_port}'}
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
            conn.request('GET',f'/api/data/video/{eid}/head.mp4',headers={'Range':'bytes=2-5'})
            response=conn.getresponse();assert response.status==206;assert response.getheader('Content-Range')=='bytes 2-5/10';assert response.read()==b'2345'
            conn.request('GET',f'/api/data/video/{eid}/head.mp4',headers={'Range':'bytes=99-'})
            response=conn.getresponse();assert response.status==416;response.read()
            conn.request('POST','/api/recording/start',body='{}',headers={'Origin':f'http://127.0.0.1:{server.server_port}','X-F1-Request':'1','Content-Type':'application/json'})
            response=conn.getresponse();assert response.status==401;response.read()
            assert app.recording.child is None and app.lease.fd is None
            (output/'escape.mp4').symlink_to(tmp_path/'outside')
            with pytest.raises(ValueError):confined_file(output,'escape.mp4')
            with pytest.raises(ValueError):confined_file(output,'../outside')
        finally:conn.close();server.shutdown();server.server_close();thread.join(1)
    finally:app.close()


def test_media_rejects_symlink_root(tmp_path):
    source=tmp_path/'real';source.mkdir();(source/'a.mp4').write_bytes(b'data')
    link=tmp_path/'linked';link.symlink_to(source,target_is_directory=True)
    with pytest.raises(ValueError):confined_file(link,'a.mp4')


def test_corrupt_lerobot_catalog_does_not_break_lxl(tmp_path):
    from data_service import DataService
    from test_data_auth import episode
    raw=tmp_path/'raw';episode(raw)
    def broken():raise ValueError('branch unavailable')
    data=DataService(raw,tmp_path/'runtime',tmp_path/'web',extra_catalog=broken)
    result=data.catalog()
    assert len(result['episodes'])==1 and result['episodes'][0]['source']=='lxl'
    assert 'branch unavailable' in result['error']

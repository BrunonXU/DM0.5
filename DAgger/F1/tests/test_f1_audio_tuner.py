import importlib.util
import json
from pathlib import Path
import threading
from urllib.request import Request,urlopen
from urllib.error import HTTPError

spec=importlib.util.spec_from_file_location('audio_tuner',Path(__file__).resolve().parents[1]/'scripts/56_recording_audio.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def test_headless_tuner_auth_gain_and_playback(tmp_path):
    entered=threading.Event();release=threading.Event();played=[]
    def player(k,v):played.append((k,v));entered.set();release.wait(3)
    config=tmp_path/'volume.json';server,token=m.make_server(('127.0.0.1',0),config,token='test-only',player=player)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def call(path,body=None,auth=True,origin=None):
        headers={'X-F1-Audio-Token':token} if auth else {}
        if origin:headers['Origin']=origin
        req=Request(f'http://127.0.0.1:{server.server_port}'+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        try:
            with urlopen(req,timeout=2) as r:return r.status,r.read()
        except HTTPError as e:return e.code,e.read()
    try:
        assert call('/')[0]==200 and not played
        assert call('/api/state',auth=False)[0]==403
        assert call('/api/play',{'kind':'started','volume':30},origin='http://other.invalid')[0]==403
        assert call('/api/play',{'kind':'bad','volume':30})[0]==400
        assert call('/api/save',{'started':101,'saved':30})[0]==400
        assert not config.exists()
        assert call('/api/save',{'started':30,'saved':40})[0]==200
        assert json.loads(config.read_text())=={'started':30,'saved':40}
        assert call('/api/play',{'kind':'started','volume':30})[0]==202
        assert entered.wait(1)
        assert call('/api/play',{'kind':'saved','volume':40})[0]==409
        assert played==[('started',30)]
        assert json.loads(call('/api/state')[1])['busy'] is True
    finally:release.set();server.shutdown();server.server_close();thread.join(2)

"""Real HTTP cache revalidation and version-safe byte ranges; no ROS."""
import http.client
import os
from pathlib import Path
import threading
from http.server import ThreadingHTTPServer
import pytest
from server import Handler

@pytest.fixture
def media(tmp_path):
    path=tmp_path/'video.mp4';path.write_bytes(bytes(range(64)))
    class Files(Handler):
        def do_GET(self):
            if not path.exists():
                self.send_error(404);return
            self.send_file(path,'video/mp4')
    server=ThreadingHTTPServer(('127.0.0.1',0),Files)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def request(headers=None):
        conn=http.client.HTTPConnection(*server.server_address,timeout=3)
        try:
            conn.request('GET','/video.mp4',headers=headers or {})
            response=conn.getresponse();return response.status,dict(response.getheaders()),response.read()
        finally:conn.close()
    yield path,request
    server.shutdown();server.server_close();thread.join(2)

def test_repeat_open_revalidates_without_body(media):
    _,get=media
    status,h,body=get();assert status==200 and len(body)==64
    assert h['Cache-Control']=='private, no-cache'
    for value in [h['ETag'],'W/'+h['ETag'],'"old", '+h['ETag'],'*']:
        status,new,body=get({'If-None-Match':value})
        assert status==304 and body==b'' and new['ETag']==h['ETag']
    assert get({'If-None-Match':'"other"'})[0]==200

def test_ranges_use_same_version_and_preserve_existing_behaviour(media):
    _,get=media;_,h,_=get();tag=h['ETag']
    status,r,body=get({'Range':'bytes=10-19','If-Range':tag})
    assert status==206 and body==bytes(range(10,20)) and r['Content-Range']=='bytes 10-19/64'
    assert get({'Range':'bytes=-3'})[2]==bytes(range(61,64))
    assert get({'Range':'bytes=999-'})[0]==416
    for stale in ['"old"','W/'+tag,'Wed, 21 Oct 2015 07:28:00 GMT']:
        status,_,body=get({'Range':'bytes=10-19','If-Range':stale})
        assert status==200 and len(body)==64

def test_same_size_same_mtime_replacement_invalidates_cache(media):
    path,get=media;_,h,_=get();old=path.stat()
    replacement=path.with_suffix('.tmp');replacement.write_bytes(b'x'*64)
    os.utime(replacement,ns=(old.st_atime_ns,old.st_mtime_ns));os.replace(replacement,path)
    status,new,body=get({'If-None-Match':h['ETag']})
    assert status==200 and body==b'x'*64 and new['ETag']!=h['ETag']
    assert get({'Range':'bytes=0-1','If-Range':h['ETag']})[0]==200
    path.unlink();assert get({'If-None-Match':new['ETag']})[0]==404

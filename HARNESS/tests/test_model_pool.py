import threading
import time
from types import SimpleNamespace
import httpx
import pytest
from f1_harness.model_pool import ModelPool, PooledModelClient
from f1_harness.model_transport import ModelTransportTrace
from f1_harness.planner import MODEL,EFFORT


def payload(): return dict(model=MODEL,reasoning={'effort':EFFORT},store=False,input=[],tools=[])
def post(client):
    trace=ModelTransportTrace()
    return client.post('/responses',json=payload(),headers={'X-Client-Request-Id':'a'*32},extensions={'trace':trace})


def test_pool_warms_without_generation_and_reuses_across_planners(tmp_path):
    calls=[]; entered=threading.Event();release=threading.Event()
    def handler(req):
        calls.append(req.method)
        if req.method=='GET':entered.set();assert release.wait(2)
        return httpx.Response(200,json={'output':[]})
    pool=ModelPool(tmp_path/'pool.sock',None,client=httpx.Client(base_url='https://example.invalid',transport=httpx.MockTransport(handler)),interval=60).start()
    try:
        assert entered.wait(1);one=PooledModelClient(pool.path,httpx.Timeout(90));assert calls==['GET']
        result=[];thread=threading.Thread(target=lambda:result.append(post(one)));thread.start()
        time.sleep(.05);assert calls==['GET']
        release.set();thread.join(2);assert result[0].status_code==200
        one.close();two=PooledModelClient(pool.path,httpx.Timeout(90));assert post(two).status_code==200
        assert calls==['GET','POST','POST'];assert not pool.prepared.get('body')
    finally:release.set();pool.close()


def test_unknown_local_result_never_becomes_retryable_connect_error():
    client=object.__new__(PooledModelClient)
    class Lost:
        def call(self,*a,**kw):raise ConnectionError('socket lost after dispatch')
    client.rpc=Lost()
    with pytest.raises(RuntimeError,match='unknown_no_replay'):post(client)


def test_proxy_tls_failure_does_not_poison_next_exchange(tmp_path, monkeypatch):
    import socket
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    connects = []
    class Proxy(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args): pass
        def do_CONNECT(self):
            connects.append(self.path)
            self.send_response(200); self.end_headers(); self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True
    server = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    clients = []
    original_client = httpx.Client
    def client_factory(**kwargs):
        client = original_client(base_url='https://example.invalid', trust_env=False,
            proxy=f'http://127.0.0.1:{server.server_port}', timeout=.3,
            limits=httpx.Limits(max_connections=1))
        clients.append(client)
        return client
    monkeypatch.setattr('f1_harness.planner.load_key', lambda _: 'fixture-not-a-key')
    monkeypatch.setattr(httpx, 'Client', client_factory)
    pool = ModelPool(tmp_path/'unused.sock', tmp_path/'unused-credential')
    try:
        with pool.gate:
            first = pool._exchange('GET', None, None)
            second = pool._exchange('GET', None, None)
        assert first['error'] == second['error'] == 'ConnectError'
        assert len(connects) == 2  # Both reached CONNECT; neither waited on a poisoned slot.
        assert all(client.is_closed for client in clients[:-1])
    finally:
        pool.client.close(); pool.server.socket.close()
        server.shutdown(); server.server_close()


def test_owned_client_reset_after_unknown_post_never_replays(tmp_path, monkeypatch):
    calls = []
    def handle(request):
        calls.append(request.method)
        request.extensions['trace']('http11.send_request_headers.started',
            {'request': SimpleNamespace(method=b'POST')})
        raise httpx.ReadTimeout('private response material')
    original_client = httpx.Client
    monkeypatch.setattr('f1_harness.planner.load_key', lambda _: 'fixture-not-a-key')
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original_client(
        base_url='https://example.invalid', transport=httpx.MockTransport(handle), trust_env=False))
    pool = ModelPool(tmp_path/'unused.sock', tmp_path/'unused-credential')
    previous = pool.client
    try:
        with pool.gate:
            result = pool._exchange('POST', payload(), 'a'*32)
        assert result['error'] == 'ReadTimeout' and result['trace']['origin_write_started']
        assert calls == ['POST'] and previous.is_closed
        assert pool.client is not previous
    finally:
        pool.client.close(); pool.server.socket.close()


def test_pool_preserves_origin_write_trace_and_sanitizes_error(tmp_path):
    def handler(req):
        if req.method=='GET':return httpx.Response(200)
        req.extensions['trace']('http11.send_request_headers.started',{'request':SimpleNamespace(method=b'POST')})
        raise httpx.ReadTimeout('SECRET-URL-AND-KEY')
    pool=ModelPool(tmp_path/'pool.sock',None,client=httpx.Client(base_url='https://example.invalid',transport=httpx.MockTransport(handler)),interval=60).start()
    try:
        c=PooledModelClient(pool.path,httpx.Timeout(90));trace=ModelTransportTrace()
        with pytest.raises(httpx.ReadTimeout) as exc:c.post('/responses',json=payload(),headers={'X-Client-Request-Id':'a'*32},extensions={'trace':trace})
        assert trace.origin_write_started and 'SECRET' not in str(exc.value)
        with pytest.raises(RuntimeError):c.rpc.call('request',payload=dict(payload(),model='unregistered'),request_id='a'*32)
    finally:pool.close()


def test_real_http_connection_survives_planner_lifetimes(tmp_path):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    peers=[];warm=threading.Event()
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args): pass
        def do_GET(self):
            peers.append(self.client_address);self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'{}');warm.set()
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']));self.do_GET()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    pool=ModelPool(tmp_path/'pool.sock',None,client=httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',trust_env=False),interval=60).start()
    try:
        assert warm.wait(2)
        for _ in range(2):
            client=PooledModelClient(pool.path,httpx.Timeout(90));trace=ModelTransportTrace()
            client.post('/responses',json=payload(),headers={'X-Client-Request-Id':'a'*32},extensions={'trace':trace});client.close()
            assert trace.summary()['connection_reused']
        assert len(set(peers))==1 and len(peers)==3
    finally:pool.close();server.shutdown();server.server_close()


def test_cancelled_caller_cannot_leave_queued_generation(tmp_path):
    import socket
    from f1_harness.ipc import send
    entered=threading.Event();release=threading.Event();calls=[]
    def handler(req):
        calls.append(req.method)
        if req.method=='GET':entered.set();assert release.wait(2)
        return httpx.Response(200,json={})
    pool=ModelPool(tmp_path/'pool.sock',None,client=httpx.Client(base_url='https://example.invalid',transport=httpx.MockTransport(handler)),interval=60).start()
    try:
        assert entered.wait(1)
        sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);sock.connect(str(pool.path))
        send(sock,dict(method='request',kwargs=dict(payload=payload(),request_id='a'*32)))
        sock.close();release.set();time.sleep(.15)
        assert calls==['GET']
    finally:release.set();pool.close()

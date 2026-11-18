"""Same-UID network pool; retains HTTP connections, never conversations or robot authority."""
import base64
import os
from pathlib import Path
import re
import threading
import time
import httpx
from .ipc import UnixClient, UnixServer
from .model_transport import ModelTransportTrace

MAXIMUM = 16 * 1024**2
ERRORS = {c.__name__: c for c in (httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout,
    httpx.ReadError, httpx.WriteTimeout, httpx.WriteError, httpx.PoolTimeout, httpx.ProxyError,
    httpx.RemoteProtocolError)}


class ModelPool:
    def __init__(self, path, credential_path, *, client=None, interval=30., subscription_path=None):
        self.path = Path(path)
        self._subscription_path = subscription_path
        self._owned_client = client is None
        self._credential_path = credential_path if client is None else None
        self.client = client or self._new_client()
        self.gate = threading.Lock()
        self.stop = threading.Event()
        self.interval = interval
        self.prepared = {}
        self.last_use = 0.
        self.server = UnixServer(self.path, self.dispatch, timeout=120, maximum=MAXIMUM, max_clients=2, client_context=True)

    def _new_client(self):
        if self._subscription_path:
            from .subscription_client import SubscriptionClient
            return SubscriptionClient(self._subscription_path)
        from .planner import load_key
        return httpx.Client(base_url='https://api.openai.com/v1',
            headers={'Authorization': 'Bearer ' + load_key(self._credential_path)},
            timeout=httpx.Timeout(90, connect=5, write=15, pool=5),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1, keepalive_expiry=120))

    def start(self):
        self.server.start()
        self.thread = threading.Thread(target=self._warm_loop, daemon=True)
        self.thread.start()
        return self

    def _warm_loop(self):
        while not self.stop.is_set():
            if time.monotonic()-self.last_use >= self.interval and self.gate.acquire(blocking=False):
                try:
                    self.prepared = self._exchange('GET', None, None)
                    self.last_use = time.monotonic()
                finally: self.gate.release()
            if self.stop.wait(self.interval): break

    def _exchange(self, method, payload, request_id):
        from .planner import MODEL
        trace = ModelTransportTrace(); started = time.monotonic()
        try:
            if method == 'GET':
                response = self.client.get('/models/'+MODEL, timeout=5., extensions={'trace':trace})
            else:
                response = self.client.post('/responses', json=payload,
                    headers={'X-Client-Request-Id':request_id}, extensions={'trace':trace})
            # Only diagnostic response headers cross the private channel.
            result = dict(status=response.status_code,
                headers={k:response.headers[k] for k in ('x-request-id','openai-processing-ms','x-f1-model-auth') if k in response.headers})
            if method == 'POST':
                if len(response.content) > MAXIMUM//2: raise ValueError('model_response_size_limit')
                result['body'] = base64.b64encode(response.content).decode()
        except httpx.HTTPError as exc:
            result = dict(error=type(exc).__name__ if type(exc).__name__ in ERRORS else 'RemoteProtocolError')
            # A failed proxy TLS upgrade can leave httpcore's sole tunnel
            # marked active forever. We hold gate: discard owned connections
            # for future requests, but never replay this exchange here.
            if self._owned_client:
                self.client.close()
                self.client = self._new_client()
        except (ValueError, RuntimeError, OSError) as exc:
            if not self._subscription_path:
                raise
            # Private auth/provider errors must never enter IPC or imply retryability.
            allowed = {'subscription_astra_not_available', 'subscription_plan_permission_missing',
                'subscription_refresh_failed_reauthorize', 'subscription_sharing_usage_limit_exceeded',
                'subscription_sharing_usage_unavailable', 'subscription_stream_incomplete_no_replay',
                'subscription_stream_failed_no_replay', 'subscription_stream_limit_no_replay'}
            result = dict(fatal=str(exc) if str(exc) in allowed else 'subscription_request_failed_no_replay')
        return dict(result, trace=trace.summary(), elapsed_s=time.monotonic()-started)

    def dispatch(self, method, args, *, client=None):
        if method == 'health' and not args:
            # No payload or authorization material is retained in this status.
            return dict(ready=True, prepared=self.prepared, generation_requested=False)
        from .planner import MODEL, EFFORT
        if method != 'request' or set(args) != {'payload','request_id'}:
            raise ValueError('model_pool_method_rejected')
        payload = args['payload']
        if (not isinstance(payload,dict) or payload.get('model') != MODEL or
            payload.get('reasoning',{}).get('effort') != EFFORT or payload.get('store') is not False or
            not re.fullmatch(r'[a-f0-9]{32}',args['request_id'])):
            raise ValueError('model_pool_request_rejected')
        started = time.monotonic()
        # Prewarm and generation cannot establish duplicate connections. The
        # waiter is bounded; a busy pool is not a reason to replay a POST.
        if not self.gate.acquire(timeout=16):
            return dict(error='PoolTimeout',trace=ModelTransportTrace().summary())
        try:
            if self.stop.is_set(): raise ValueError('model_pool_closing')
            if client is not None:
                import select, socket
                if select.select([client],[],[],0)[0]:
                    # A terminated planner must not leave a queued generation.
                    if not client.recv(1,socket.MSG_PEEK|socket.MSG_DONTWAIT):
                        raise ValueError('model_pool_caller_disconnected_before_dispatch')
                    raise ValueError('model_pool_unexpected_pipelined_data')
            value = self._exchange('POST',payload,args['request_id'])
            value['pool_wait_s'] = time.monotonic()-started-value.get('elapsed_s',0)
            self.last_use = time.monotonic()
            return value
        finally:self.gate.release()

    def close(self):
        self.stop.set(); self.server.close()
        if hasattr(self,'thread'): self.thread.join(timeout=16)
        # A planner may have exited while a request was in flight. Never use
        # its response to execute a tool. Close after the bounded I/O finishes.
        def finish():
            with self.gate: self.client.close()
        threading.Thread(target=finish,daemon=True).start()


class PooledModelClient:
    def __init__(self,path,timeout):
        self.rpc=UnixClient(Path(path),timeout=120,maximum=MAXIMUM)
        self.timeout=timeout
        self.health=self.rpc.call('health')  # Before any generation; fail here may use direct transport.

    def post(self,path,*,json,headers,extensions):
        assert path=='/responses'
        try: result=self.rpc.call('request',payload=json,request_id=headers['X-Client-Request-Id'])
        except (OSError,RuntimeError,ValueError):
            # Dispatch may already have reached the provider: no HTTPError and
            # no fallback/retry, even if this was a local socket disconnect.
            raise RuntimeError('model_pool_result_unknown_no_replay') from None
        trace=extensions['trace'];summary=result.get('trace',{})
        trace.phase=summary.get('last_phase','pool')
        trace.origin_write_started=summary.get('origin_write_started',False)
        trace.summary=lambda: dict(summary,pool_wait_s=result.get('pool_wait_s',0))
        if result.get('fatal'):
            raise RuntimeError(result['fatal'])
        if result.get('error'):
            raise ERRORS.get(result['error'],httpx.RemoteProtocolError)('model_pool_transport_failure')
        return httpx.Response(result['status'],content=base64.b64decode(result['body'],validate=True),headers=result['headers'])

    def close(self): pass  # The service, not this run, owns the network connection.

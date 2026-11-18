"""Redacted httpcore phase timings, including HTTPS proxy CONNECT negotiation."""
import time


class ModelTransportTrace:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.pending = {}
        self.durations = {}
        self.phase = 'pool'
        self.proxy_exchange = False
        self.origin_write_started = False
        self.network_events = 0
        self.connections = 0
        self.proxy_timeout = None

    def __call__(self, event, info):
        operation, _, state = event.rpartition('.')
        if state not in ('started', 'complete', 'failed'):
            return
        kind = operation.rsplit('.', 1)[-1]
        if kind not in ('connect_tcp', 'start_tls', 'send_request_headers',
                        'send_request_body', 'receive_response_headers', 'receive_response_body'):
            return
        self.network_events += 1
        if state == 'started':
            if kind == 'connect_tcp':
                phase = 'tcp_connect'
                self.connections += 1
            elif kind == 'start_tls':
                phase = 'tls_connect'
                self.proxy_exchange = False
                self._restore_timeout()
            elif kind == 'send_request_headers':
                request = info.get('request')
                self.proxy_exchange = getattr(request, 'method', None) == b'CONNECT'
                if self.proxy_exchange:
                    # httpcore uses the model's read timeout while waiting for
                    # CONNECT. Bound this establishment step by connect timeout,
                    # then restore the full model read budget before origin I/O.
                    limits = request.extensions.get('timeout', {})
                    original = limits.get('read')
                    connect = limits.get('connect')
                    self.proxy_timeout = (limits, original)
                    if connect is not None:
                        limits['read'] = min(original, connect) if original is not None else connect
                    phase = 'proxy_connect'
                else:
                    self._restore_timeout()
                    self.origin_write_started = True
                    phase = 'request_write'
            elif self.proxy_exchange:
                phase = 'proxy_connect'
            else:
                phase = {'send_request_body': 'request_write',
                         'receive_response_headers': 'response_headers',
                         'receive_response_body': 'response_body'}[kind]
            self.phase = phase
            self.pending[operation] = (phase, self.clock())
        elif operation in self.pending:
            phase, started = self.pending.pop(operation)
            self.durations[phase] = self.durations.get(phase, 0.) + self.clock() - started

    def _restore_timeout(self):
        if self.proxy_timeout:
            limits, original = self.proxy_timeout
            limits['read'] = original
            self.proxy_timeout = None

    def summary(self):
        self._restore_timeout()
        return dict(phase_s=dict(self.durations), last_phase=self.phase,
                    origin_write_started=self.origin_write_started,
                    connection_reused=self.network_events > 0 and self.connections == 0,
                    network_trace_available=self.network_events > 0)


def request_headers(response):
    """Only the provider's documented diagnostic headers, never cookies/body."""
    result = {}
    if response.headers.get('x-f1-model-auth') == 'chatgpt_subscription':
        result['model_auth_source'] = 'chatgpt_subscription'
    request_id = response.headers.get('x-request-id', '')
    if request_id and len(request_id) <= 200 and all(c.isalnum() or c in '-_' for c in request_id):
        result['provider_request_id'] = request_id
    processing = response.headers.get('openai-processing-ms', '')
    if processing.isascii() and processing.isdigit() and len(processing) <= 10:
        result['provider_processing_ms'] = int(processing)
    return result

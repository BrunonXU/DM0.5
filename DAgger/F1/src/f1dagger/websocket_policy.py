"""F1 EEF synchronous policy: persistent binary WS in a bounded child process."""
import json
import multiprocessing
import signal
import struct
import threading
import time
import uuid

from .policy import HttpPolicy
from .policy_errors import IKRejected

PROTOCOL = 'f1_eef_sync_v2'


def encode_request(metadata, frames):
    meta = dict(metadata)
    meta['frames'] = [dict(camera=f.camera, encoding=f.media_type, width=f.width, height=f.height,
                           length=len(f.payload), host_received_monotonic_ns=f.host_received_monotonic_ns)
                      for f in frames]
    header = json.dumps(meta, allow_nan=False, separators=(',', ':')).encode()
    if len(header) > 65536 or sum(len(f.payload) for f in frames) > 12*1024**2:
        raise ValueError('policy payload too large')
    return struct.pack('!I', len(header)) + header + b''.join(f.payload for f in frames)


def network_worker(pipe, endpoint, timeout):
    """No ROS/owner objects cross this boundary. A failed request is never replayed."""
    # The foreground parent owns Ctrl+C, robot stop and recording finalization.
    # This IPC-only child exits on pipe EOF (or the parent's bounded terminate).
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    from websockets.sync.client import connect
    connection = None
    try:
        while True:
            payload = pipe.recv_bytes(12*1024**2 + 65540)
            stages = dict(child_received_ns=time.monotonic_ns())
            started = time.perf_counter()
            try:
                if connection is None:
                    connection = connect(endpoint, compression=None, proxy=None, open_timeout=timeout,
                                         close_timeout=.2, max_size=4*1024**2, max_queue=1)
                stages['ws_send_started_ns'] = time.monotonic_ns()
                if payload:
                    connection.send(payload)
                    stages['ws_send_completed_ns'] = time.monotonic_ns()
                    response = connection.recv(timeout=timeout)
                    stages['ws_received_ns'] = time.monotonic_ns()
                    if not isinstance(response, bytes):
                        raise ValueError('binary response required')
                    result = json.loads(response)
                else:
                    # Local IPC command: establish transport, send no inference
                    # request and grant no control authority.
                    result = dict(transport_ready=True)
                stages['child_decoded_ns'] = time.monotonic_ns()
                result['client_network_rtt_ms'] = (time.perf_counter()-started)*1000
            except Exception as exc:
                if connection is not None:
                    connection.close()
                connection = None
                result = dict(success=False, error=f'{type(exc).__name__}: {exc}')
            stages['child_reply_started_ns'] = time.monotonic_ns()
            result['client_transport_stages'] = stages
            pipe.send_bytes(json.dumps(result, allow_nan=False).encode())
    except (EOFError, BrokenPipeError):
        pass
    finally:
        if connection is not None:
            connection.close()
        pipe.close()


class WebSocketPolicy(HttpPolicy):
    right_only = True
    websocket_path = '/ws'

    def __init__(self, config, task, validate_action, *, held_getter, clock=time.monotonic_ns):
        super().__init__(config, task, validate_action, clock=clock)
        self.held_getter = held_getter
        self.session_id = uuid.uuid4().hex
        self.process = self.pipe = None
        self.inflight = threading.Lock()
        self.last_timing = {}

    def health(self):
        result = super().health()
        if (result.get('protocol') != PROTOCOL or result.get('rtc') is not False
                or result.get('prediction_horizon') != 50 or result.get('execute_steps') != 20
                or self.config['chunk_steps'] != 20):
            raise ValueError('EEF sync protocol capabilities mismatch')
        return result

    def close(self):
        if self.pipe is not None:
            self.pipe.close(); self.pipe = None
        if self.process is not None:
            self.process.join(timeout=.2)
            if self.process.is_alive():
                self.process.terminate(); self.process.join(timeout=1)
            self.process = None

    def prepare(self):
        """Warm only the network process/connection before operator START."""
        if self.model_id is None or not self.inflight.acquire(blocking=False):
            raise ValueError('policy not bound or already in flight')
        try:
            result = self._exchange(b'')
            if result.get('transport_ready') is not True:
                raise ValueError('policy transport preparation failed: ' + str(result.get('error'))[:500])
        except Exception:
            self.close()
            raise
        finally:
            self.inflight.release()

    def _exchange(self, payload):
        stages = dict(exchange_started_ns=time.monotonic_ns())
        if self.process is None:
            context = multiprocessing.get_context('spawn')
            self.pipe, child = context.Pipe()
            endpoint = self.endpoint.replace('https://', 'wss://', 1).replace('http://', 'ws://', 1) + self.websocket_path
            self.process = context.Process(target=network_worker, args=(child, endpoint, self.config['timeout_s']), daemon=True)
            self.process.start(); child.close()
        stages['parent_send_started_ns'] = time.monotonic_ns()
        self.pipe.send_bytes(payload)
        stages['parent_send_completed_ns'] = time.monotonic_ns()
        if not self.pipe.poll(self.config['timeout_s'] + 1):
            self.close()
            raise TimeoutError('policy network worker expired')
        response = self.pipe.recv_bytes(4*1024**2)
        stages['parent_received_ns'] = time.monotonic_ns()
        result = json.loads(response)
        stages['parent_decoded_ns'] = time.monotonic_ns()
        result.setdefault('client_transport_stages', {}).update(stages)
        return result

    def infer(self, obs, request_id, epoch):
        if self.model_id is None or not self.inflight.acquire(blocking=False):
            raise ValueError('policy not bound or already in flight')
        started = time.perf_counter()
        packed = None
        result = {}
        outcome, verified = 'error', False
        self.last_timing = {}
        try:
            metadata = dict(contract='f1_upper_16d_v1', protocol=PROTOCOL, session_id=self.session_id,
                request_id=request_id, observation_id=obs.observation_id, policy_epoch=epoch,
                observation_monotonic_ns=obs.capture_started_monotonic_ns,
                state16=[float(value) for value in obs.state16], last_submitted16=self.held_getter().tolist(),
                state_timestamps_ns=list(obs.state_timestamps_ns), chunk_steps=20, execute_steps=20,
                prediction_horizon=50, frequency_hz=30., model_id=self.model_id, model_version=self.model_version)
            payload = encode_request(metadata, obs.frames)
            packed = time.perf_counter()
            result = self._exchange(payload)
            ik_rejected = result.get('success') is False and result.get('reason') == 'ik_rejected' and 'error' not in result
            if result.get('success') is not True and not ik_rejected:
                raise ValueError('policy rejected: ' + str(result.get('error', result.get('reason'))))
            for key in ('protocol', 'session_id', 'prediction_horizon', 'execute_steps'):
                if result.get(key) != metadata[key]:
                    raise ValueError('policy identity mismatch: ' + key)
            if result.get('shadow_only') or result.get('active_action_indices') != [7,8,9,10,11,12,13,15] or result.get('rtc') is not False:
                raise ValueError('unexpected action mask/RTC')
            self.validate_identity(result, metadata, obs)
            if ik_rejected:
                details = result.get('diagnostics')
                if (result.get('actions') is not None or not isinstance(details, list)
                        or not 1 <= len(details) <= 20
                        or any(not isinstance(d, dict) or d.get('success') is not True for d in details[:-1])
                        or not isinstance(details[-1], dict) or details[-1].get('success') is not False
                        or details[-1].get('joints') is not None):
                    raise ValueError('invalid IK rejection response')
                outcome, verified = 'ik_rejected', True
                raise IKRejected(result)
            actions = self.validate(result, metadata, obs)
            outcome, verified = 'success', True
            return actions
        except IKRejected:
            raise  # Keep the verified persistent connection for a NEW request.
        except Exception:
            self.close()
            raise
        finally:
            self.last_timing = dict(request_id=request_id, policy_epoch=epoch,
                session_id=self.session_id, outcome=outcome, response_verified=verified,
                pack_ms=None if packed is None else (packed-started)*1000,
                client_total_ms=(time.perf_counter()-started)*1000,
                network_rtt_ms=result.get('client_network_rtt_ms') if isinstance(result, dict) else None,
                server=result.get('timing_ms') if verified else None,
                observation_to_response_ms=(self.clock()-obs.capture_started_monotonic_ns)/1e6)
            self.inflight.release()

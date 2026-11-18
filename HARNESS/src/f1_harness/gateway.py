"""Loopback-only RPent JSON HTTP façade; no button injection or raw actuator API."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from rpent.utils.rpc.http_rpc import HttpRpcServer, _HttpRpcHandler

from .contracts import HarnessError, SKILLS

OWNER_METHODS = frozenset({"open_session", "heartbeat", "prepare_task", "prepare_skill", "submit_skill",
                          "cancel_skill", "skill_result", "finish_task", "close_session", "status", "shadow_probe", "execution_events",
                          "preview_skill", "geometry_context", "hybrid_control", "register_program_trial"})


class Gateway:
    def __init__(self, engine, observer, store):
        self.engine, self.observer, self.store = engine, observer, store

    def dispatch(self, method, args=(), kwargs=None, **transport):
        kwargs = kwargs or {}
        if args:
            raise HarnessError("positional_RPC_arguments_forbidden")
        if method in OWNER_METHODS:
            value=getattr(self.engine, method)(**kwargs)
            if method=='status' and hasattr(self.observer,'readiness'):
                value=dict(value,observation_readiness=self.observer.readiness())
            return value
        if method == "capabilities" and not kwargs:
            return dict(schema="f1-harness-gateway-v1", mode=self.engine.mode.value, skills=list(SKILLS),
                        hardware_ready=self.engine.hardware_ready, raw_motion=False,
                        consistent_observation_snapshot=True,
                        program_trial_registration=True,
                        calibration_required=True, training_ready=False)
        if method == "observe" and not kwargs:
            observation = self.observer.observe()
            self.engine.update_observation(observation)
            from .scene_evidence import observation_wire
            return observation_wire(observation,include_measured_gripper=True)
        if method == 'camera_observation' and not kwargs:
            # Do not update the owner: image-only data is not a calibrated scene.
            return self.observer.camera_observation()
        if method == 'aperture_observation' and not kwargs:
            return self.observer.aperture_observation()
        if method == 'locate_target':
            return self.observer.locate_target(**kwargs)
        if method=='observe_with_status' and not kwargs:
            observation=self.observer.observe()
            self.engine.update_observation(observation)
            status=self.engine.status()
            if hasattr(self.observer,'readiness'):
                status=dict(status,observation_readiness=self.observer.readiness())
            from .scene_evidence import observation_wire
            return dict(observation=observation_wire(observation,include_measured_gripper=True),status=status)
        if method == "events" and set(kwargs) <= {"after"}:
            after = int(kwargs.get("after", 0))
            if after < 0:
                raise HarnessError("invalid_event_offset")
            path = self.store.root / "trace.jsonl"
            if not path.exists():
                return dict(events=[], next=after)
            # Tail-sized response; original journal stays authoritative.
            with path.open() as stream:
                rows = [json.loads(line) for index, line in enumerate(stream) if after <= index < after + 100]
            return dict(events=rows, next=after + len(rows))
        raise HarnessError("RPC_method_not_available")


class BoundedHandler(_HttpRpcHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(3.)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 < length <= 65536 or self.headers.get("Transfer-Encoding"):
            self.send_error(413, "request size rejected")
            return
        super().do_POST()


class GatewayServer:
    def __init__(self, gateway: Gateway, *, port=0):
        self.server = HttpRpcServer(("127.0.0.1", port), gateway.dispatch)
        self.server.RequestHandlerClass = BoundedHandler
        self.gateway = gateway
        self.thread = None
        self.stop = threading.Event()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        threading.Thread(target=self._watch, daemon=True).start()
        return self

    def _watch(self):
        while not self.stop.wait(.05):
            self.gateway.engine.tick()

    def close(self):
        self.stop.set()
        try:
            self.gateway.engine.revoke("gateway_closed")
        finally:
            self.server.shutdown()
            self.server.server_close()
            if self.thread:
                self.thread.join(timeout=1)


def blob_server(store, *, port=0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            try:
                if not self.path.startswith("/blobs/"):
                    raise ValueError("unknown route")
                data = store.blob(self.path.removeprefix("/blobs/"))
                if len(data) > 32 * 1024 * 1024:
                    raise ValueError("blob too large")
            except (ValueError, OSError):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server

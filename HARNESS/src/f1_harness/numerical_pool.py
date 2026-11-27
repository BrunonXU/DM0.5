"""Optional fixed-release numerical worker. No owner, observations or publishers.

A run keeps its own numerical endpoint and leases only the preloaded IK/FK
models. Busy cancellation poisons the worker; late results are discarded. No
request is transparently retried after dispatch. An abandoned lease prevents
reuse until the supervisor restarts this optional worker.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

from .contracts import Task, digest
from .ipc import UnixClient, UnixServer

METHODS = frozenset(('solve_path', 'forward_pose', 'forward_path', 'endpoint_ik'))
# Native joint search has a 15-second computation budget. The transport must
# outlive it, including serialization, so warm and cold routes return the same
# typed solver result instead of turning a legal search into an RPC timeout.
NUMERICAL_RPC_TIMEOUT_S = 18.
WARM_NUMERICAL_RPC_TIMEOUT_S = 17.
# Best-effort revocation must still fit inside the outer RPC after an inner
# timeout. Failure to receive this cleanup reply never makes a lease reusable.
TRANSPORT_FAILURE_RELEASE_TIMEOUT_S = .5


class FixedFiles:
    """Bind declared executable and native asset bytes at initialization/acquire."""
    def __init__(self, files):
        self.files = {Path(p): h for p, h in files.items()}
        self.check()
        self.identity = digest({str(p): h for p, h in self.files.items()})

    def check(self):
        for path, expected in self.files.items():
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError('warm_numerical_source_changed')

    @classmethod
    def release(cls, bundle, spec):
        bundle = Path(bundle).resolve()
        manifest_path = bundle / 'manifest.json'
        raw = manifest_path.read_bytes(); manifest = json.loads(raw)
        files = manifest['files']
        if hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest() != manifest['bundle_id']:
            raise ValueError('warm_numerical_manifest_invalid')
        bound = {manifest_path: hashlib.sha256(raw).hexdigest()}
        for name, expected in files.items():
            path = bundle / name
            if not path.resolve().is_relative_to(bundle):
                raise ValueError('warm_numerical_manifest_path')
            if name.startswith(('src/', 'control/', 'native/', 'upstream/', 'configs/')):
                bound[path] = expected
        config = spec['gate']['config']
        infra = Path(config['_infra_root']).resolve()
        # The existing F1 dependency binding includes mapping, model meshes and
        # native protection sources. Do not infer asset identity from paths.
        dependency = bundle / 'control' / config['infra_manifest']
        if not dependency.resolve().is_relative_to(bundle / 'control'):
            raise ValueError('warm_numerical_dependency_path')
        raw = dependency.read_bytes()
        if hashlib.sha256(raw).hexdigest() != config['_infra_manifest_sha256']:
            raise ValueError('warm_numerical_dependency_binding_changed')
        bound[dependency] = hashlib.sha256(raw).hexdigest()
        data = json.loads(raw)
        for name, expected in data['files'].items():
            path = infra / name
            if not path.resolve().is_relative_to(infra):
                raise ValueError('warm_numerical_asset_path')
            bound[path] = expected
        if Path(spec['urdf']).resolve() not in bound or infra / 'configs/model_mapping.json' not in bound:
            raise ValueError('warm_numerical_model_unbound')
        return cls(bound)


class NumericalPool:
    def __init__(self, spec, binding, *, factory=None, runtime=None):
        if factory is None:
            from .numerical_service import NumericalDispatcher
            factory = NumericalDispatcher
        self.binding = binding
        self.spec_id = digest(spec)
        self.lock = threading.Lock()
        self.active = None
        self.inflight = 0
        self.poisoned = False
        self.serial = 0
        started = time.monotonic()
        native_spec = deepcopy(spec)
        # Preserve exact requested configuration identity, but isolate generated
        # model/cache files from a cold fallback after an abandoned/busy lease.
        self.runtime = None if runtime is None else str(Path(runtime).resolve())
        if self.runtime is not None:
            native_spec['gate']['runtime'] = self.runtime
        self.numeric = factory(native_spec)
        binding.check()
        self.startup_s = time.monotonic() - started

    def dispatch(self, method, kwargs):
        if method == 'acquire':
            with self.lock:
                if self.poisoned or self.active is not None:
                    return dict(acquired=False, reason='warm_numerical_unavailable')
                if set(kwargs) != {'spec_id', 'binding_id', 'task'}:
                    raise ValueError('warm_numerical_acquire_fields')
                task = Task.model_validate(kwargs['task'])
                if kwargs['spec_id'] != self.spec_id or kwargs['binding_id'] != self.binding.identity:
                    return dict(acquired=False, reason='warm_numerical_identity_mismatch')
                try:
                    self.binding.check()
                except (OSError, ValueError):
                    self.poisoned = True
                    return dict(acquired=False, reason='warm_numerical_source_changed')
                self.serial += 1
                self.active = dict(token=uuid.uuid4().hex, serial=self.serial,
                                   task_hash=digest(task), task_id=task.task_id)
                return dict(acquired=True, **self.active, initialization_s=self.startup_s,
                            binding_id=self.binding.identity, command_publishers=0)
        with self.lock:
            active = self.active
            if (active is None or kwargs.get('lease') != active):
                raise ValueError('warm_numerical_lease_expired')
            if method == 'release':
                self.active = None
                if self.inflight:
                    self.poisoned = True
                else:
                    # Reset task-local IK/diagnostics. Every solve anchors anew
                    # from its supplied measurements; FK overwrites all state.
                    try:self.numeric.clear_run()
                    except Exception:
                        self.poisoned = True
                        raise
                return dict(released=True, reusable=not self.poisoned)
            if (method != 'call' or self.poisoned or kwargs.get('operation') not in METHODS
                    or not isinstance(kwargs.get('kwargs'), dict)):
                raise ValueError('warm_numerical_method_forbidden')
            self.inflight += 1
        try:
            result = self.numeric.dispatch(kwargs['operation'], kwargs['kwargs'])
            with self.lock:
                if self.active != active or self.poisoned:
                    raise ValueError('warm_numerical_result_revoked')
            return result
        finally:
            with self.lock:
                self.inflight -= 1


class NumericalLease:
    def __init__(self, client, lease, receipt):
        self.client, self.lease, self.receipt = client, lease, receipt
        self.closed = False
        self.lock = threading.Lock()

    def dispatch(self, method, kwargs):
        with self.lock:
            if self.closed or method not in METHODS:
                raise ValueError('warm_numerical_lease_closed_or_method_forbidden')
        # Each RPC gets its own connection; FK and IK retain independent lanes.
        try:
            result = self.client.call('call', lease=self.lease, operation=method, kwargs=kwargs)
        except OSError:
            # The native call may still be running after a disconnected or
            # timed-out socket. Revoke this lease instead of queueing another
            # request behind unknown computation or replaying it cold. Keep
            # the original transport exception even if cleanup also fails.
            try:self.close(timeout=TRANSPORT_FAILURE_RELEASE_TIMEOUT_S)
            except (OSError, ValueError, RuntimeError):pass
            raise
        with self.lock:
            if self.closed:
                raise ValueError('warm_numerical_result_revoked')
        return result

    def close(self, *, timeout=1.):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        client = UnixClient(Path(self.client.path), timeout=timeout)
        client.call('release', lease=self.lease)


def acquire(socket, config_path, *, bundle):
    """Cold fallback only at initialization, before any numerical dispatch."""
    begin = time.monotonic()
    config_path = Path(config_path)
    config = json.loads(config_path.read_text())
    binding = FixedFiles.release(bundle, config['spec'])
    task = Task.model_validate_json((config_path.parent / 'task.local.json').read_text())
    client = UnixClient(Path(socket), timeout=1.)
    result = client.call('acquire', spec_id=digest(config['spec']),
                         binding_id=binding.identity, task=task.model_dump(mode='json'))
    if result.get('acquired') is not True:
        return None, dict(reused=False, reason=result.get('reason'), elapsed_s=time.monotonic()-begin)
    lease = {key: result[key] for key in ('token', 'serial', 'task_hash', 'task_id')}
    if lease['task_hash'] != digest(task) or lease['task_id'] != task.task_id or result['binding_id'] != binding.identity:
        raise ValueError('warm_numerical_reply_binding_mismatch')
    client.timeout = WARM_NUMERICAL_RPC_TIMEOUT_S
    receipt = dict(reused=True, serial=lease['serial'], task_id=task.task_id,
                   task_hash=lease['task_hash'], binding_id=binding.identity,
                   initialization_s=result['initialization_s'], elapsed_s=time.monotonic()-begin)
    return NumericalLease(client, lease, receipt), receipt


def serve_pool(site, socket, store, *, bundle, stdin=None):
    import sys
    from .mesh_cache import register_mesh_cache
    from .numerical_service import NUMERICAL_CLIENTS
    site = Path(site)
    config = json.loads((site / 'numerics.local.json').read_text())
    binding = FixedFiles.release(bundle, config['spec'])
    register_mesh_cache(site, store, bundle=bundle)
    pool = NumericalPool(config['spec'], binding, runtime=store.root/'native-model')
    ended = threading.Event()
    def watch():
        # Supervisor owns stdin. EOF/disconnect ends this numerical-only worker.
        try:
            while (stdin or sys.stdin).buffer.read(1):
                pass
        finally:
            ended.set()
    threading.Thread(target=watch, daemon=True).start()
    server = UnixServer(Path(socket), pool.dispatch, max_clients=NUMERICAL_CLIENTS+2,
                        timeout=NUMERICAL_RPC_TIMEOUT_S).start()
    try:
        event = dict(service='numerical_pool', initialization_s=pool.startup_s,
                     binding_id=binding.identity, numerical_runtime=pool.runtime,
                     command_publishers=0, motion_authorized=False)
        store.event('service_ready', **event)
        print(json.dumps(dict(ready=True, **event)), flush=True)
        ended.wait()
    finally:
        server.close()

"""Fail-closed bwrap worker supervisor with an external wall-clock deadline."""
from pathlib import Path
import os
import signal
import subprocess
import tempfile
import time

from pyrualean.arms import ArmResult

from .ipc import UnixClient, UnixServer


class SandboxedArm:
    def __init__(self, backend, workspace: Path, *, timeout_s=35,program_timeout_s=30):
        if not 0<program_timeout_s<=300:raise ValueError('worker_program_budget_out_of_range')
        self.backend, self.timeout_s = backend, timeout_s
        self.program_timeout_s=program_timeout_s
        self.source_root = Path(__file__).resolve().parents[2]
        # Frozen coordinator code may share the existing interpreter. Mount its
        # real installation paths, but import only this immutable source copy.
        self.root = (self.source_root / '.venv').resolve().parent
        self.channel_tmp = tempfile.TemporaryDirectory(prefix="f1-channel-")
        self.channel = Path(self.channel_tmp.name)
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.process = None
        self.server = None
        self.turns = []
        self._image_blobs = {}
        self._program_execution = None

    def _backend_call(self, method, kwargs):
        if method == 'artifact_chunk':
            import base64
            data=self._image_blobs[kwargs['sha256']]
            offset=kwargs['offset']
            if type(offset) is not int or not 0<=offset<len(data):
                raise ValueError('invalid_artifact_offset')
            return base64.b64encode(data[offset:offset+24000]).decode()
        if method not in {"execute_tool", "solved", "env_steps", "cancel", "artifact"}:
            raise ValueError("backend_method_unavailable")
        execution = self._program_execution if method == 'execute_tool' else None
        if execution is not None:
            # This is host-side RPC evidence. Worker stdout and claimed request
            # identifiers are never used as execution provenance.
            import json
            call = dict(ordinal=len(execution['calls']), name=kwargs['name'],
                        input_sha256=execution['store'].put_blob(json.dumps(
                            kwargs['input_dict'], sort_keys=True, allow_nan=False).encode()))
            execution['calls'].append(call)
            execution['store'].event('program_backend_call', **execution['identity'], **call)
        try:
            result = getattr(self.backend, method)(**kwargs)
        except Exception as exc:
            if execution is not None:
                call['exception_type'] = type(exc).__name__
                execution['store'].event('program_backend_exception', **execution['identity'], **call)
            raise
        if method == "execute_tool":
            result = getattr(result, "result", result)
            if execution is not None:
                call['result_sha256'] = execution['store'].put_blob(json.dumps(
                    result, sort_keys=True, allow_nan=False).encode())
                # Only the authoritative response's direct skill receipt is
                # indexed; the complete response remains available as a blob.
                if isinstance(result, dict) and isinstance(result.get('request_id'), str):
                    call['request_id'] = result['request_id']
                chain = getattr(self.backend, 'last_request_chain', None)
                if kwargs['name'] == 'execute_skill' and chain:
                    call['request_chain_sha256'] = execution['store'].put_blob(json.dumps(
                        chain, sort_keys=True, allow_nan=False).encode())
                execution['store'].event('program_backend_result', **execution['identity'], **call)
        if method == "artifact":
            import hashlib
            if not isinstance(result,bytes) or not 0<len(result)<=8*1024**2:
                raise ValueError('image_artifact_size_invalid')
            sha=hashlib.sha256(result).hexdigest()
            self._image_blobs[sha]=result
            while len(self._image_blobs)>2:self._image_blobs.pop(next(iter(self._image_blobs)))
            return dict(sha256=sha,size=len(result))
        return result

    def start(self):
        self.server = UnixServer(self.channel / "backend.sock", self._backend_call,
                                 timeout=self.program_timeout_s+5).start()
        command = ["/usr/bin/bwrap", "--unshare-all", "--die-with-parent", "--new-session",
                   "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
        # Mount only interpreter/libraries and project code. The project root, API.MD,
        # user home, host /proc, hardware device nodes and network are never mounted.
        for path in (Path("/usr/lib"), Path("/lib"), Path("/lib64"), self.root / ".python",
                     self.root / ".venv", self.source_root / "src", self.root / ".overlay"):
            if path.exists():
                command.extend(["--ro-bind", str(path), str(path)])
        command.extend(["--bind", str(self.channel), "/channel", "--bind", str(self.workspace), "/task",
                        "--chdir", "/task", "--setenv", "HOME", "/task/.home",
                        "--setenv", "PYTHONPATH", f"{self.source_root}/src:{self.root}/.overlay",
                        "--setenv", "F1_WORKER_TIMEOUT", str(self.program_timeout_s),
                        str(self.root / ".venv/bin/python"), "-m", "f1_harness.worker"])
        self.error_file = (self.workspace / "worker.stderr").open("wb")
        self.process = subprocess.Popen(command, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                                                     "OPENBLAS_NUM_THREADS": "1"},
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=self.error_file, start_new_session=True)
        until = time.monotonic() + 10
        while not (self.channel / "worker.sock").exists():
            if self.process.poll() is not None or time.monotonic() > until:
                self.close()
                raise RuntimeError("isolated_worker_unavailable; inspect worker.stderr (no fallback)")
            time.sleep(.02)
        self.client = UnixClient(self.channel / "worker.sock", timeout=self.timeout_s)
        return self

    def get_tools_spec(self):
        return self.client.call("get_tools_spec")

    def execute_tool(self, name, input_dict, *, _fresh_namespace=False):
        try:
            value = self.client.call("execute_candidate" if _fresh_namespace else "execute_tool",
                                     name=name, input_dict=input_dict)
            self.turns.extend(value["turns"])
            import hashlib
            images=[]
            if len(value.get('images',[]))>2:raise ValueError('too_many_feedback_images')
            for item in value.get('images',[]):
                path=(self.workspace/item['file']).resolve()
                if path.parent!=self.workspace or path.stat().st_size>8*1024**2:
                    raise ValueError('invalid_feedback_image_path')
                data=path.read_bytes()
                if hashlib.sha256(data).hexdigest()!=item['sha256']:
                    raise ValueError('feedback_image_identity_mismatch')
                images.append(data);path.unlink()
            return ArmResult(value["result"],images)
        except (OSError, RuntimeError):
            self.close()
            raise RuntimeError("worker_failed_or_deadline; authority_revoked") from None

    def execute_candidate(self, value, store, *, call_id, request_id):
        """Execute exact source in the existing sandbox, retaining host receipts.

        A returned cell is not proof that a physical goal succeeded. The binding
        deliberately makes no verification or automatic-promotion decision.
        """
        import uuid
        from .program_experience import validate_candidate, invocation_code
        candidate = validate_candidate(value)
        code = invocation_code(candidate)
        if self._program_execution is not None:
            raise RuntimeError('program_execution_already_active')
        identity = dict(invocation_id=uuid.uuid4().hex, source_sha256=candidate['source_sha256'],
                        code_sha256=store.put_blob(code.encode()),
                        source_blob_sha256=store.put_blob(candidate['source'].encode()),
                        call_id=call_id, model_request_id=request_id,
                        origin='sandbox_host')
        execution = dict(identity=identity, store=store, calls=[])
        store.event('program_execution_started', **identity, verified=False)
        self._program_execution = execution
        status = 'worker_failed'
        try:
            result = self.execute_tool('python', dict(code=code), _fresh_namespace=True)
            status = 'cell_error' if result.result.get('exception') or result.result.get('error') else 'cell_returned'
            return result, dict(identity, status=status, verified=False,
                                backend_calls=execution['calls'],
                                request_ids=[c['request_id'] for c in execution['calls'] if 'request_id' in c])
        finally:
            self._program_execution = None
            store.event('program_execution_finished', **identity, status=status, verified=False,
                        backend_calls=execution['calls'],
                        request_ids=[c['request_id'] for c in execution['calls'] if 'request_id' in c])

    def audit(self):
        return self.client.call("audit")

    def close(self):
        if self.process and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=3)
        try:
            self.backend.cancel()
        finally:
            if self.server:
                self.server.close()
            if hasattr(self, "error_file"):
                self.error_file.close()
            self.channel_tmp.cleanup()

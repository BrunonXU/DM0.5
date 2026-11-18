"""Credential-free PyRUA interpreter. Launched only inside the OS sandbox."""
from pathlib import Path
import time

from .adapters import F1Robot, F1ArmToolkit
from .ipc import UnixClient, UnixServer


class RemoteBackend:
    def __init__(self, path, *, timeout_s=32):
        self.client = UnixClient(path, timeout=timeout_s)

    def execute_tool(self, name, input_dict):
        return self.client.call("execute_tool", name=name, input_dict=input_dict)

    def solved(self):
        return self.client.call("solved")

    def env_steps(self):
        return self.client.call("env_steps")

    def artifact(self, name, step=-1):
        import base64,hashlib
        meta=self.client.call("artifact",name=name,step=step)
        if not 0<meta['size']<=8*1024**2:raise ValueError('image_artifact_size_invalid')
        data=bytearray()
        while len(data)<meta['size']:
            chunk=base64.b64decode(self.client.call('artifact_chunk',sha256=meta['sha256'],offset=len(data)),validate=True)
            if not chunk or len(data)+len(chunk)>meta['size']:raise ValueError('invalid_artifact_chunk')
            data.extend(chunk)
        if hashlib.sha256(data).hexdigest()!=meta['sha256']:raise ValueError('artifact_identity_mismatch')
        return bytes(data)

    def cancel(self):
        return self.client.call("cancel")


def main():
    import os
    import resource
    import math
    import sys
    from types import ModuleType
    from .program_experience import MATH_PROGRAM_EXPORTS
    # Preserve the original pure functions before any model-written cell runs.
    # A new globals dictionary alone would still inherit a mutated math module
    # through sys.modules. Only host code can access this saved export mapping.
    original_math={name:getattr(math,name) for name in MATH_PROGRAM_EXPORTS if hasattr(math,name)}
    resource.setrlimit(resource.RLIMIT_AS,(4*1024**3,4*1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE,(64*1024**2,64*1024**2))
    timeout=float(os.environ.get('F1_WORKER_TIMEOUT','30'))
    if not 0<timeout<=300:raise ValueError('invalid_worker_deadline')
    # Registered transfer allows 90 s plus 30 s read-only observation acquisition.
    # The external program deadline and owner heartbeat remain independent.
    backend = RemoteBackend(Path("/channel/backend.sock"),timeout_s=timeout+2)
    arm = F1ArmToolkit(F1Robot(backend), arm="cells", workspace=Path("/task"),
                     images='on-demand', max_turns=40, program_timeout_s=timeout)

    def dispatch(method, kwargs):
        if method == "get_tools_spec":
            return arm.get_tools_spec()
        if method in ("execute_tool", "execute_candidate"):
            active_arm = arm
            previous_math = None
            if method == 'execute_candidate':
                if kwargs.get('name') != 'python':
                    raise ValueError('candidate_python_only')
                # A host-only invocation has fresh globals and RobotBase cache;
                # it shares the existing transport/owner, not mutable helpers.
                # The persistent arm is never replaced, even on exceptions.
                active_arm = F1ArmToolkit(F1Robot(backend), arm='cells', workspace=Path('/task'),
                    images='on-demand', max_turns=1, program_timeout_s=timeout)
                previous_math=sys.modules['math']
                isolated_math=ModuleType('math')
                isolated_math.__dict__.update(original_math)
                sys.modules['math']=isolated_math
            try:
                result = active_arm.execute_tool(**kwargs)
            finally:
                if previous_math is not None:sys.modules['math']=previous_math
            import hashlib,uuid
            images=[]
            for data in result.images[:2]:
                if len(data)>8*1024**2:raise ValueError('feedback_image_too_large')
                name='feedback-'+uuid.uuid4().hex+'.png'
                Path('/task',name).write_bytes(data)
                images.append(dict(file=name,sha256=hashlib.sha256(data).hexdigest()))
            return dict(result=result.result, turns=active_arm.turns[-1:], images=images)
        if method == "audit":
            import os
            return dict(environment_keys=sorted(os.environ), pid=os.getpid(),
                        host_home_visible=Path("/home/user/.ssh").exists(),
                        credential_visible=Path("/home/user/projects/HARNESS/API.MD").exists(),
                        devices=sorted(p.name for p in Path("/dev").iterdir()))
        raise ValueError("worker_method_unavailable")

    server = UnixServer(Path("/channel/worker.sock"), dispatch, timeout=timeout+10).start()
    try:
        while True:
            time.sleep(1)
    finally:
        server.close()


if __name__ == "__main__":
    main()

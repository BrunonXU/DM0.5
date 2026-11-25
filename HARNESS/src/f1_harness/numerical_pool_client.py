"""Supervisor-owned optional numerical prewarm; no run or START is created."""
import importlib.util
from pathlib import Path
import shlex
import subprocess
import uuid

from .collect import ssh_reader_command
from .site import _safe_environment


class WarmNumericalClient:
    def __init__(self, config, runtime):
        self.config, self.runtime = config, Path(runtime)
        self.process = self.log = None
        self.socket = '/home/user/Bruno/HARNESS/runtime/np-' + uuid.uuid4().hex + '.sock'

    def start(self):
        cfg = self.config
        root = Path(cfg['root'])
        spec = importlib.util.spec_from_file_location('_numerical_pool_launcher', root / 'scripts/start_development.py')
        helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
        ssh = ssh_reader_command({}, host=cfg['host'], port=cfg['port'],
            identity=cfg['identity'], known_hosts=cfg['known_hosts'])[:-1]
        template = '/home/user/Bruno/HARNESS/site/' + Path(cfg['local_site']).name
        argv = ['/home/user/Bruno/DAgger-runtime/f1-09-venv/bin/python',
                cfg['remote_bundle'] + '/native/entry.py', '--run',
                '/home/user/Bruno/HARNESS/runs/np-' + uuid.uuid4().hex,
                'numerical-pool', template, '--socket', self.socket]
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.log = (self.runtime / 'numerical-pool.log').open('ab')
        try:
            self.process = subprocess.Popen(ssh + [shlex.join(['python3', '-c', helper.REMOTE_GUARD, *argv])],
                stdin=subprocess.PIPE, stdout=self.log, stderr=subprocess.STDOUT,
                env=_safe_environment(), start_new_session=True)
        except BaseException:
            self.log.close(); raise
        return self.socket

    def close(self):
        process = self.process
        if process is not None:
            if process.stdin:
                process.stdin.close()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                try: process.wait(timeout=2)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=2)
        if self.log is not None:
            self.log.close()

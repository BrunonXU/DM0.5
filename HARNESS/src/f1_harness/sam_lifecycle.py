"""Warm SAM beside the coordinator, before any user task starts."""
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request


class WarmSam:
    def __init__(self,config,runtime):
        self.config=config;self.runtime=Path(runtime);self.child=None;self.log=None

    def start(self):
        settings=self.config['sam_service'];root=Path(self.config['root'])
        self.log=(self.runtime/'sam-service.log').open('ab')
        environment={'PATH':os.environ.get('PATH','/usr/bin:/bin'),
            'PYTHONPATH':str(root/'src'),'CUDA_VISIBLE_DEVICES':settings['gpu_uuid'],
            'OMP_NUM_THREADS':'2','PYTHONUNBUFFERED':'1'}
        self.child=subprocess.Popen([settings['python'],'-m','f1_harness.sam_service',
            '--config',settings['config']],stdin=subprocess.DEVNULL,stdout=self.log,
            stderr=subprocess.STDOUT,env=environment,start_new_session=True)
        expected=json.loads(Path(settings['config']).read_text())['checkpoint_sha256']
        for _ in range(300):
            if self.child.poll() is not None:raise RuntimeError('sam_service_exited')
            try:
                with urllib.request.urlopen('http://127.0.0.1:19842/health',timeout=.2) as response:
                    health=json.load(response)
                if health.get('ready') and health.get('model_sha256')==expected and health.get('pid')==self.child.pid:
                    (self.runtime/'sam-ready.json').write_text(json.dumps(health)+'\n')
                    return health
            except (OSError,ValueError):pass
            time.sleep(.1)
        raise RuntimeError('sam_service_initialization_timeout')

    def close(self):
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try:self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:self.child.kill();self.child.wait(timeout=5)
        if self.log:self.log.close()

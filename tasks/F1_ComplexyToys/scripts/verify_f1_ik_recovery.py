#!/usr/bin/env python3
"""F1 ARM fault-injection acceptance: forced sync-shadow, never powered.

Real feedback/cameras/owner computation; FOUR synthetic IK rejections followed
by measured hold chunks. Parent supplies Enter only after the pause prompt.
This verifies recovery plumbing, not model or physical motion quality.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True)
    p.add_argument('--runtime', required=True)
    p.add_argument('--child', action='store_true')
    a = p.parse_args()
    if not a.child:
        child = subprocess.Popen([sys.executable, '-u', __file__, '--child',
            '--config', a.config, '--runtime', a.runtime], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        resumes = 0
        try:
            for line in child.stdout:
                print(line, end='', flush=True)
                if 'IK连续拒绝，已保持当前位置并暂停。' in line:
                    resumes += 1
                    child.stdin.write('\n'); child.stdin.flush()
            code = child.wait(timeout=10)
            if code or resumes != 1:
                raise RuntimeError(f'shadow recovery failed: exit={code}, resumes={resumes}')
            print(json.dumps(dict(shadow_recovery='PASS', enter_resumes=resumes, synthetic_policy=True)))
        finally:
            if child.poll() is None:
                child.terminate(); child.wait(timeout=10)
        return 0
    import numpy as np
    from f1dagger.policy_errors import IKRejected
    from f1dagger.websocket_policy import WebSocketPolicy
    from f1dagger import cli
    count = 0
    def injected(self, obs, request_id, epoch):
        nonlocal count
        count += 1
        if count <= 4:
            raise IKRejected(dict(request_id=request_id, observation_id=obs.observation_id,
                diagnostics=[dict(success=False, joints=None, synthetic_test=True)]))
        result = np.tile(self.held_getter(), (20, 1))
        result[:, 7:14] = np.asarray(obs.state16)[7:14]
        return result
    WebSocketPolicy.infer = injected
    sys.argv = ['f1dagger', 'sync-shadow', '--duration-seconds', '5', '--config', a.config,
                '--runtime', a.runtime, '--purpose', 'policy_hil', '--video', 'off',
                '--data-root', str(Path(a.runtime) / 'synthetic-shadow-data'),
                '--recording-mode', 'hil-record', '--task', 'SYNTHETIC IK recovery SHADOW acceptance']
    return cli.main()


if __name__ == '__main__':
    raise SystemExit(main())

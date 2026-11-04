"""6000D coordinator: hold F1's control lock while switching our H20 services."""
import argparse
import json
from pathlib import Path
import select
import shlex
import subprocess


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    projects = Path(__file__).resolve().parents[3]
    code = ("import fcntl,sys; "
            "f=open('/home/user/Bruno/F1/runtime/control.lock','a+'); "
            "fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB); "
            "print('LOCKED',flush=True); sys.stdin.read()")
    lock = subprocess.Popen(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
        '-F', str(projects/'F1-4242/results/pose_task_20260928/ssh_config'),
        'f1-09-deploy', 'python3 -c '+shlex.quote(code)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        if not select.select([lock.stdout], [], [], 15)[0] or lock.stdout.readline().strip() != 'LOCKED':
            raise RuntimeError('Could not acquire F1 control lock; no service switch attempted')
        print('F1 control lock acquired; no robot motion can start during service switch.', flush=True)
        for step in ('50000', '5000'):
            command = ['/u01/user/envs/f1-jimu-pi05-v1/bin/python',
                '/home/user/projects/task-releases/f1-toys-20261020-rtc-math-candidate/activate_rtc_fast_h20.py', step]
            result = subprocess.run(['ssh', '-o', 'BatchMode=yes', 'h20-train', shlex.join(command)],
                                    text=True, capture_output=True, timeout=300)
            (args.output/f'{step}.log').write_text(result.stdout+result.stderr)
            print(result.stdout, flush=True)
            assert result.returncode == 0, result.stderr
        (args.output/'result.json').write_text(json.dumps(dict(services_switched=True, robot_commands=0)))
    finally:
        lock.stdin.close()
        try:
            lock.wait(timeout=10)
        except subprocess.TimeoutExpired:
            lock.terminate()
            lock.wait(timeout=10)


if __name__ == '__main__':
    main()

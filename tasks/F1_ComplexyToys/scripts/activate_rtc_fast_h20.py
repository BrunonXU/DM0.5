"""H20-only switch of our named RTC service; caller must hold F1 control lock."""
import argparse
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time
from urllib.request import urlopen


def health(port):
    with urlopen(f'http://127.0.0.1:{port}/health', timeout=2) as stream:
        return json.load(stream)


def stop_owned(session, release, bundle):
    pane_pid = int(subprocess.check_output(
        ['tmux', 'display-message', '-p', '-t', session, '#{pane_pid}'], text=True).strip())
    children = subprocess.run(['ps', '-o', 'pid=', '--ppid', str(pane_pid)],
                              text=True, capture_output=True).stdout.split()
    matches = []
    for candidate in (pane_pid, *map(int, children)):
        proc = Path(f'/proc/{candidate}')
        command = (proc/'cmdline').read_bytes().split(b'\0')
        if (proc.stat().st_uid == os.getuid()
                and any(str(release).encode() in arg for arg in command)
                and str(bundle).encode() in command):
            matches.append(candidate)
    assert len(matches) == 1, (session, matches)
    pid = matches[0]
    proc = Path(f'/proc/{pid}')
    assert proc.stat().st_uid == os.getuid()
    command = (proc/'cmdline').read_bytes().split(b'\0')
    assert any(str(release).encode() in arg for arg in command), command
    assert str(bundle).encode() in command, command
    os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        if not proc.exists():
            return pid
        time.sleep(.1)
    raise RuntimeError('Own service did not exit; no force kill attempted')


def start(session, script, console):
    command = f'exec bash {shlex.quote(str(script))} > {shlex.quote(str(console))} 2>&1'
    subprocess.run(['tmux', 'new-session', '-d', '-s', session, command], check=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('checkpoint', choices=('5000', '50000'))
    args = p.parse_args()
    step = args.checkpoint
    port, gpu = (8137, 0) if step == '5000' else (8138, 1)
    base = Path('/home/user/projects/task-releases/f1-toys-20261020-rtc-h20-v3')
    target = base.with_name('f1-toys-20261020-rtc-h20-v4-fastmath')
    bundle = Path(f'/u01/user/runs/f1-toys-v1-formal-20261016-r3/bundle-{step}')
    oldrun = Path(f'/u01/user/runs/f1-toys-rtc-h20-{step}-graph-20261020')
    newrun = oldrun.with_name(f'f1-toys-rtc-h20-{step}-fast-20261020')
    oldsession, newsession = f'f1-rtc-h20-{step}-graph', f'f1-rtc-h20-{step}-fast'
    assert f'export CUDA_VISIBLE_DEVICES={gpu}\n' in (newrun/'start.sh').read_text()
    before = health(port)
    assert before['ready'] and before['model_id'].endswith(
        'ff1263050a8b6846' if step == '5000' else '1d55a3e0017742b0')
    oldpid = stop_owned(oldsession, base, bundle)
    print(json.dumps({'stopped_owned_pid': oldpid, 'checkpoint': step}), flush=True)
    start(newsession, newrun/'start.sh', newrun/'console.log')
    try:
        deadline = time.monotonic()+240
        while time.monotonic() < deadline:
            try:
                after = health(port)
                assert after['model_version'] == before['model_version']
                assert after['rtc_optimization'].endswith('_native_math_v1')
                assert after['ready']
                break
            except (OSError, AssertionError):
                time.sleep(1)
        else:
            raise TimeoutError('Candidate health timeout')
    except BaseException:
        if subprocess.run(['tmux', 'has-session', '-t', newsession], capture_output=True).returncode == 0:
            stop_owned(newsession, target, bundle)
        start(oldsession, oldrun/'start-v3.sh', oldrun/'console-rollback.log')
        raise
    receipt = dict(checkpoint=step, gpu=gpu, old_pid=oldpid, before=before, after=after,
                   rollback_start=str(oldrun/'start-v3.sh'), robot_commands=0)
    (newrun/'activation.json').write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt), flush=True)


if __name__ == '__main__':
    main()

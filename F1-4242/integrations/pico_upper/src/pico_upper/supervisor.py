"""Own only this session; share a control lock with the 4242 console."""
from __future__ import annotations
import argparse
import ctypes
import fcntl
import json
import os
from pathlib import Path
import signal
import select
import socket
import subprocess
import sys
import time


def process_start(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def stop(runtime):
    path = runtime / 'session.json'
    if not path.exists():
        print('PICO_UPPER_NOT_RUNNING (PCService and other controllers untouched)')
        return 0
    state = json.loads(path.read_text())
    pid = int(state['pid'])
    if process_start(pid) != state.get('process_start'):
        print('PICO_UPPER_SESSION_STALE no_process_killed=true')
        return 1
    cmd = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    if b'pico_upper.supervisor' not in cmd or b'start' not in cmd:
        raise RuntimeError('session identity mismatch; no signal sent')
    os.kill(pid, signal.SIGTERM)
    print(f'PICO_UPPER_STOP_REQUESTED pid={pid}; no reset/base zero/PCService stop')
    return 0



def wait_for_enter(stream, cancelled):
    """An idle confirmation prompt remains cancellable by Ctrl+C or stop.sh."""
    while not cancelled():
        readable, _, _ = select.select([stream], [], [], .1)
        if readable:
            if stream.readline() == '':
                raise RuntimeError('LOCAL_OPERATOR_TERMINAL_CLOSED')
            return
    raise KeyboardInterrupt


def terminate_owned_groups(children):
    """Clean child process groups even if their leader exited before its helpers."""
    for child in children:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def start(runtime):
    root = Path(os.environ['PICO_F1_ROOT'])
    lock_path = Path(os.environ['PICO_UPPER_CONTROL_LOCK'])
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open('a+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError('CONTROL_OCCUPIED: 4242 console or another Pico session owns control')
    state = {'owner':'pico_upper','pid':os.getpid(),'process_start':process_start(os.getpid()),
             'started_at':time.time(),'mode':'arms_and_grippers','source_root':str(root)}
    lock.seek(0); lock.truncate(); json.dump(state, lock); lock.flush()
    state_path = runtime / 'session.json'
    state_path.write_text(json.dumps(state, indent=2)+'\n')
    children = []
    handles = []
    stopping = False
    def on_signal(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    parent_pid = os.getpid()
    def death_signal():
        # Linux F1: child exits if supervisor dies, including SIGKILL.
        if ctypes.CDLL(None).prctl(1, signal.SIGTERM) != 0 or os.getppid() != parent_pid:
            os._exit(1)
    def spawn(args, label, log=False):
        output = None
        if log:
            path = runtime / 'logs' / f'{label}.log'
            output = path.open('a', buffering=1)
            handles.append(output)
        child = subprocess.Popen([sys.executable, *args], cwd=root, stdout=output,
                    stderr=subprocess.STDOUT if log else None, start_new_session=True,
                    preexec_fn=death_signal, pass_fds=(lock.fileno(),))
        children.append(child)
        return child
    def wait(child):
        while child.poll() is None:
            if stopping:
                raise KeyboardInterrupt
            time.sleep(.1)
        if child.returncode != 0:
            raise RuntimeError(f'START_STAGE_FAILED exit={child.returncode}')
    try:
        for port in (60061,63901):
            with socket.create_connection(('127.0.0.1',port), timeout=2):
                pass
        print('复用已运行 PCService；不改旧lxl、不启动相机/数采/上传。', flush=True)
        wait(spawn(['-m','pico_upper.preflight'], 'preflight'))
        print('确认机器人周围可操作，松开两侧 Grip/扳机并保持摇杆居中。按 Enter 继续，Ctrl+C 取消。', flush=True)
        # Only local operator opens the command stream.
        if not sys.stdin.isatty():
            raise RuntimeError('LOCAL_OPERATOR_TERMINAL_REQUIRED')
        wait_for_enter(sys.stdin, lambda: stopping)
        if stopping:
            raise KeyboardInterrupt
        wait(spawn([str(root/'scripts/wait_for_xr.py')], 'xr_wait'))
        # Re-capture after any headset waiting time. This remains read-only.
        wait(spawn(['-m','pico_upper.preflight'], 'preflight'))
        bridge = spawn(['-m','pico_upper.bridge'], 'bridge', log=True)
        time.sleep(.4)
        if bridge.poll() is not None:
            raise RuntimeError('bridge exited; see runtime/logs/bridge.log')
        teleop = spawn(['-m','pico_upper.teleop'], 'teleop', log=True)
        print(f'PICO_UPPER_STARTING logs={runtime}/logs; 等待日志 UPPER_TELEOP_READY 后接管。', flush=True)
        print('Ctrl+C 或另一终端 ./stop.sh 结束本会话。ABXY/摇杆/腰部/录制均关闭。', flush=True)
        while not stopping:
            for name, child in [('bridge',bridge),('teleop',teleop)]:
                if child.poll() is not None:
                    raise RuntimeError(f'{name} exited ({child.returncode}); see runtime/logs/{name}.log')
            time.sleep(.2)
    except KeyboardInterrupt:
        print('PICO_UPPER_CANCELLED', flush=True)
    finally:
        terminate_owned_groups(children)
        deadline = time.monotonic()+3
        for child in children:
            try:
                child.wait(timeout=max(.1,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        for handle in handles:
            handle.close()
        if state_path.exists() and json.loads(state_path.read_text()).get('pid') == os.getpid():
            state_path.unlink()
        lock.seek(0); lock.truncate(); lock.flush()
        lock.close()
        print('PICO_UPPER_STOPPED own_processes_only=true posture_reset=false', flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['start','stop'])
    args = parser.parse_args()
    runtime = Path(os.environ['PICO_UPPER_RUNTIME'])
    runtime.mkdir(parents=True, exist_ok=True)
    try:
        if args.action == 'start':
            if os.environ.get('SSH_CONNECTION') or os.environ.get('SSH_TTY'):
                raise RuntimeError('SSH_MOTION_START_DISABLED; use the local F1 operator terminal')
            return start(runtime)
        return stop(runtime)
    except Exception as exc:
        print(f'PICO_UPPER_ERROR {exc}',file=sys.stderr,flush=True)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())

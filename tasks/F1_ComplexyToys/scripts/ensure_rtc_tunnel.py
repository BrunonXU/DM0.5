"""F1-only direct RTC tunnel; no robot API or command publisher."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen

RUNTIME = Path('/home/user/Bruno/DAgger-runtime/f1-toys-20261016')
COMMAND = ['ssh', '-F', '/home/user/Bruno/connect/ssh_config', '-NT',
    '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
    '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=5',
    '-o', 'ServerAliveCountMax=3', '-o', 'ConnectTimeout=5',
    '-L', '127.0.0.1:8136:127.0.0.1:8135', '6000d-f1-09-tunnel']


def health():
    with urlopen('http://127.0.0.1:8136/health', timeout=2) as response:
        result = json.load(response)
    if not result.get('ready') or result.get('protocol') != 'f1_eef_rtc_v2':
        raise RuntimeError('RTC tunnel endpoint identity mismatch')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--supervise', action='store_true')
    a = p.parse_args()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if a.supervise:
        lock = (RUNTIME/'rtc-direct-supervisor.lock').open('a+')
        try:fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:return
        (RUNTIME/'rtc-direct-supervisor.pid').write_text(str(os.getpid()))
        while True:
            subprocess.run(COMMAND, stdin=subprocess.DEVNULL, check=False)
            time.sleep(2)
    else:
        try:
            health()
        except Exception:
            with (RUNTIME/'rtc-direct-tunnel.log').open('ab') as log:
                subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--supervise'],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            for _ in range(15):
                time.sleep(1)
                try:health();break
                except Exception:pass
            else:raise RuntimeError('RTC direct tunnel unavailable; inspect rtc-direct-tunnel.log')
        print('RTC direct link ready: F1 8136 -> 6000D 8135', flush=True)


if __name__ == '__main__':
    main()

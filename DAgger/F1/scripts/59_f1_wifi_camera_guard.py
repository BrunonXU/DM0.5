#!/usr/bin/env python3
"""Temporary F1-09 camera multicast TTL guard; no robot commands or restart."""
import argparse
from datetime import datetime, timezone
import json
import os
import signal
from pathlib import Path
import subprocess
import time

MACHINE_ID = '90429b8a96e04ef79dbc12175b538115'
TAG = 'f1-dagger-camera-wifi-20260928'
# These are the three camera sockets identified by the on-site header capture.
CAMERAS = {52404: 35219, 57993: 38221, 59332: 32374}
TABLE = 'f1_dagger_wifi_camera_20260928'
RULESET = f"""create table ip {TABLE}
add chain ip {TABLE} output {{ type route hook output priority mangle; policy accept; }}
add rule ip {TABLE} output oifname "wlan0" ip saddr 192.168.31.58 ip daddr 239.255.0.1 udp sport {{ 52404, 57993, 59332 }} udp dport 7401 counter ip ttl set 0 comment "{TAG}"
"""


def command(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): {result.stderr.strip()}")
    return result


def change_rule(op):
    if op == '-I':
        return command(['/usr/sbin/nft', '-f', '-'], input=RULESET)
    if op == '-D':
        return command(['/usr/sbin/nft', 'delete', 'table', 'ip', TABLE])
    raise ValueError(op)


def present():
    tables = json.loads(command(['/usr/sbin/nft', '-j', 'list', 'tables']).stdout)
    return any(entry.get('table', {}).get('family') == 'ip'
               and entry.get('table', {}).get('name') == TABLE
               for entry in tables.get('nftables', []))


def fresh_observation(path, after):
    with path.open('rb') as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - 32768))
        lines = stream.read().splitlines()
    for line in reversed(lines):
        try: row = json.loads(line)
        except (ValueError, UnicodeDecodeError): continue
        if row.get('capture_started_monotonic_ns', 0) <= after: continue
        stamps = row.get('state_timestamps_ns', [])
        frames = row.get('frames', [])
        if len(stamps) != 16 or min(stamps) <= after: continue
        if {f.get('camera') for f in frames} != {'head', 'left_wrist', 'right_wrist'}: continue
        if min(f.get('host_received_monotonic_ns', 0) for f in frames) <= after: continue
        if time.monotonic_ns() - row.get('available_monotonic_ns', 0) > 1_000_000_000: continue
        return row['observation_id']
    return None


def guarded_install(verify):
    if present(): raise RuntimeError('Rule already installed; inspect status or explicitly rollback.')
    try:
        change_rule('-I')
        if not present(): raise RuntimeError('Rule was not installed')
        return verify()
    except BaseException:
        if present(): change_rule('-D')
        raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('apply', 'rollback'))
    p.add_argument('--observations', type=Path)
    args = p.parse_args()
    if os.geteuid() != 0: p.error('run locally with sudo')
    if Path('/etc/machine-id').read_text().strip() != MACHINE_ID: p.error('not F1-09')
    if args.action == 'rollback':
        if present(): change_rule('-D')
        print('Removed only the tagged camera TTL rule; other rules unchanged.')
        return
    if not args.observations: p.error('--observations is required')
    path = args.observations.resolve()
    if Path('/home/user/Bruno/DAgger-runtime') not in path.parents or not path.is_file():
        p.error('observation file must exist in DAgger runtime')
    sockets = subprocess.check_output(['ss', '-unap'], text=True)
    for port, pid in CAMERAS.items():
        if not any(f'192.168.31.58:{port} ' in line and f'pid={pid},' in line for line in sockets.splitlines()):
            raise RuntimeError(f'Camera socket changed: {port}/{pid}; repeat capture before applying')
    if fresh_observation(path, time.monotonic_ns()-2_000_000_000) is None:
        raise RuntimeError('Start shadow and wait for fresh three-camera/state observations first')
    # Ask the running kernel to validate the complete atomic transaction first.
    command(['/usr/sbin/nft', '-c', '-f', '-'], input=RULESET)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    backup = Path('/home/user/Bruno/DAgger-runtime/network-guard')/stamp
    backup.mkdir(parents=True, exist_ok=False)
    (backup/'nft-before.txt').write_text(command(['/usr/sbin/nft', 'list', 'ruleset']).stdout)
    (backup/'rule.nft').write_text(RULESET)
    def verify():
        applied_ns = time.monotonic_ns()
        deadline = time.monotonic()+8
        while time.monotonic() < deadline:
            row = fresh_observation(path, applied_ns+1_000_000_000)
            if row is not None: return row
            time.sleep(.2)
        raise RuntimeError('No fresh three-camera/state observation after rule; automatically rolled back')
    def interrupted(*_): raise KeyboardInterrupt('network guard interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    observation_id = guarded_install(verify)
    report = dict(ok=True,tag=TAG,table=TABLE,rule=RULESET,observation_file=str(path),observation_id=observation_id,
                  temporary=True,robot_commands=False,processes_restarted=False)
    (backup/'result.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report, indent=2))
    print('Evidence:', backup)
    print('Rollback: sudo python3 /home/user/Bruno/DAgger-runtime/59_f1_wifi_camera_guard.py rollback')

if __name__ == '__main__': main()

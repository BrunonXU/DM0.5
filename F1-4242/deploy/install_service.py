#!/usr/bin/env python3
"""Install this workbench's user service from an activated release, while idle."""
from pathlib import Path
import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from project_paths import installation_environment, profile_path, read_profile

UNIT = 'f1-console-4242.service'


def command(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout.strip()


def unit_quote(value):
    return json.dumps(str(value).replace('%', '%%'), ensure_ascii=False)


def service_text(base, profile, runtime, control_lock, port):
    values = {'F1_INFRA_HOME': str(base), 'F1_PROFILE_PATH': str(profile),
              'F1_CONSOLE_RUNTIME': str(runtime), 'F1_CONTROL_LOCK': str(control_lock),
              'F1_CONSOLE_PORT': str(port)}
    environment = '\n'.join('Environment=' + unit_quote(k + '=' + v) for k, v in values.items())
    return f'''[Unit]
Description=F1-infra workbench (no automatic motion or recording)
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory={unit_quote(base / 'current')}
ExecStart={unit_quote(base / 'current/run.sh')}
{environment}
Restart=always
RestartSec=5
TimeoutStopSec=20
CPUAccounting=yes
MemoryAccounting=yes
IOAccounting=yes

[Install]
WantedBy=default.target
'''


def main():
    env = installation_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path(env['F1_INFRA_HOME']))
    parser.add_argument('--profile', type=Path, default=profile_path())
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--control-lock', type=Path)
    parser.add_argument('--port', type=int, default=int(os.environ.get('F1_CONSOLE_PORT', '4242')))
    args = parser.parse_args()
    base = args.home.expanduser().absolute()
    runtime = (args.runtime or Path(os.environ.get('F1_CONSOLE_RUNTIME', str(base / 'runtime')))).expanduser().absolute()
    lock_path = (args.control_lock or Path(os.environ.get('F1_CONTROL_LOCK', str(runtime / 'control.lock')))).expanduser().absolute()
    assert base.resolve() == base, 'installation root must not be a symlink'
    assert ROOT == (base / 'current').resolve(), 'activate this release under --home/current before installation'
    assert 1 <= args.port <= 65535, 'invalid HTTP port'
    profile = read_profile(args.profile)
    assert Path('/etc/machine-id').read_text().strip() == profile['expected_machine_id'], 'selected profile machine identity mismatch'
    assert command('loginctl', 'show-user', str(os.getuid()), '-p', 'Linger', '--value') == 'yes', 'enable user lingering first'
    from recording_service import RecordingService
    recorder = RecordingService(runtime, ROOT)
    assert recorder.state()['mode'] in ('not_started', 'unavailable'), 'finish/stop this recorder before service replacement'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+') as lease_guard:
        fcntl.flock(lease_guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = command('systemctl', '--user', 'show', UNIT, '-p', 'ActiveState', '--value')
        if state in ('active', 'activating', 'reloading'):
            with urllib.request.urlopen(f'http://127.0.0.1:{args.port}/api/state', timeout=5) as response:
                status = json.load(response)
            assert not status['control']['stopping']
            assert not status['action'] or status['action']['status'] in ('succeeded', 'failed', 'rejected', 'cancelled'), 'active action'
        backup = runtime / 'service_backups' / time.strftime('%Y%m%dT%H%M%S')
        backup.mkdir(parents=True, exist_ok=False)
        old = subprocess.run(['systemctl', '--user', 'cat', UNIT], capture_output=True, text=True)
        (backup / 'previous_unit.txt').write_text(old.stdout + old.stderr)
        unit_dir = base / 'systemd'
        unit_dir.mkdir(exist_ok=True)
        target = unit_dir / UNIT
        if target.exists():
            shutil.copy2(target, backup / 'previous_owned_unit.service')
        user_link = Path.home() / '.config/systemd/user' / UNIT
        if user_link.exists() or user_link.is_symlink():
            (backup / 'user_entry.json').write_text(json.dumps({'path': str(user_link), 'symlink': str(user_link.readlink()) if user_link.is_symlink() else None}))
            if user_link.is_file():
                shutil.copy2(user_link, backup / 'previous_user_entry.service')
            if not user_link.is_symlink() or user_link.resolve() != target.resolve():
                raise RuntimeError('Existing user unit differs; saved backup, review before replacing it')
        selected_profile = args.profile.expanduser().resolve()
        if selected_profile.is_relative_to(ROOT):
            selected_profile = base / 'current' / selected_profile.relative_to(ROOT)
        tmp = target.with_suffix('.tmp')
        tmp.write_text(service_text(base, selected_profile, runtime, lock_path, args.port))
        os.replace(tmp, target)
        if state != 'inactive':
            command('systemctl', '--user', 'stop', UNIT)
        command('systemctl', '--user', 'daemon-reload')
        command('systemctl', '--user', 'enable', str(target))
        command('systemctl', '--user', 'daemon-reload')
        command('systemctl', '--user', 'start', UNIT)
        result = {'installed': str(target), 'backup': str(backup), 'enabled': command('systemctl', '--user', 'is-enabled', UNIT), 'active': command('systemctl', '--user', 'is-active', UNIT), 'reboot_tested': False, 'robot_commands_sent': 0}
        (backup / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result))


if __name__ == '__main__':
    main()

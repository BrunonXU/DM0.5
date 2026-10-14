#!/usr/bin/env python3
"""Install Whereweare's existing user service, keeping its compatible unit name."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

MARKER = "# Managed by Projects-Hub/deploy/install_user_service.py"


def quote(value):
    return '"' + str(value).replace('%', '%%').replace('\\', '\\\\').replace('"', '\\"') + '"'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true', help='Write the user unit and start it; default only prints the unit')
    parser.add_argument('--port', type=int, default=4343)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be between 1024 and 65535')
    root = Path(__file__).resolve().parents[1]
    runtime = root / 'runtime'
    command = [sys.executable, root / 'server.py', '--workspace', root.parent, '--runtime', runtime, '--host', '127.0.0.1', '--port', str(args.port)]
    unit = '\n'.join([
        MARKER, '[Unit]', 'Description=Whereweare project review workspace', 'After=network.target', '',
        '[Service]', 'Type=simple', 'WorkingDirectory=' + str(root).replace('%', '%%'),
        'ExecStart=' + ' '.join(quote(x) for x in command), 'Restart=on-failure', 'RestartSec=3',
        'TimeoutStopSec=10', 'UMask=0077',
        'StandardOutput=append:' + str(runtime / 'server.log'),
        'StandardError=append:' + str(runtime / 'server.log'), '',
        '[Install]', 'WantedBy=default.target', ''
    ])
    if not args.install:
        print(unit, end='')
        return
    if not (root / 'server.py').is_file():
        raise SystemExit('server.py is missing; finish and verify the application first')
    config_root = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config')))
    target = config_root / 'systemd' / 'user' / 'projects-hub.service'
    if target.exists() and not target.read_text().startswith(MARKER):
        raise SystemExit('An unmanaged projects-hub.service already exists; it was not overwritten')
    subprocess.run(['systemctl', '--user', 'show-environment'], check=True, stdout=subprocess.DEVNULL)
    with tempfile.TemporaryDirectory(prefix='projects-hub-unit-') as unit_dir:
        candidate = Path(unit_dir) / 'projects-hub.service'
        candidate.write_text(unit)
        subprocess.run(['systemd-analyze', '--user', 'verify', str(candidate)], check=True)
    runtime.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = runtime / 'projects-hub.service.previous'
        backup.write_bytes(target.read_bytes())
    temp = target.with_suffix('.service.tmp')
    temp.write_text(unit)
    temp.replace(target)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'enable', '--now', 'projects-hub.service'], check=True)
    subprocess.run(['systemctl', '--user', 'is-active', 'projects-hub.service'], check=True)
    print('Installed ' + str(target))
    print('Local URL: http://127.0.0.1:' + str(args.port))
    print('Log: ' + str(runtime / 'server.log'))


if __name__ == '__main__':
    main()

#!/usr/bin/python3
"""Install only this user's desktop/share routing. Does not restart sessions."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess


def main():
    source = Path(__file__).resolve().parent
    home = Path.home()
    launcher = str(home / '.local/bin/hq-files')
    if any(c in launcher for c in ('"', '`', '$', '\\', '\n', '%')):
        raise SystemExit('Unsupported home path for desktop Exec; no changes made.')
    desktop = Path('/usr/share/applications/org.gnome.Nautilus.desktop').read_text()
    assert desktop.count('DBusActivatable=true') == 1
    assert desktop.count('Exec=nautilus --new-window %U') == 1
    assert desktop.count('Exec=nautilus --new-window\n') == 1
    desktop = desktop.replace('DBusActivatable=true', 'DBusActivatable=false')
    desktop = desktop.replace('Exec=nautilus --new-window', f'Exec="{launcher}" --new-window')
    payloads = {
        '.local/bin/hq-files': ((source / 'hq-files').read_bytes(), 0o755),
        '.local/bin/hq-share-autoconnect': ((source / 'hq-share-autoconnect.py').read_bytes(), 0o755),
        '.local/share/applications/org.gnome.Nautilus.desktop': (desktop.encode(), 0o644),
        '.local/share/applications/hq-company-share.desktop': (f'''[Desktop Entry]
Type=Application
Name=机器人共享盘
Comment=打开已保存登录的公司共享盘
Exec="{launcher}" --share
Icon=folder-remote
Terminal=false
StartupNotify=true
Categories=Network;FileManager;
'''.encode(), 0o644),
        '.config/autostart/hq-desktop-binding.desktop': (f'''[Desktop Entry]
Type=Application
Name=Personal desktop file connection
Exec="{launcher}" --session-start
NoDisplay=true
X-GNOME-Autostart-enabled=true
'''.encode(), 0o644),
    }
    for name in ('hq-desktop.service', 'hq-share-autoconnect.service', 'hq-share-autoconnect.timer'):
        payloads[f'.config/systemd/user/{name}'] = ((source / name).read_bytes(), 0o644)
    # Refuse to replace any pre-existing user customization on first install.
    collisions = [str(home / p) for p in payloads if (home / p).exists() or (home / p).is_symlink()]
    if collisions:
        raise SystemExit('Existing files require review; no changes made: ' + ', '.join(collisions))
    records = []
    for relative, (data, mode) in payloads.items():
        dest = home / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open('xb') as stream:
            stream.write(data)
        dest.chmod(mode)
        records.append({'path': str(dest), 'sha256': hashlib.sha256(data).hexdigest(), 'created': True})
    state = home / '.local/state/hq-desktop-binding'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt = {'installed_utc': datetime.now(timezone.utc).isoformat(), 'files': records,
               'desktop_service_started': False, 'existing_desktop_preserved': True}
    (state / 'installation.json').write_text(json.dumps(receipt, indent=2) + '\n')
    env = os.environ.copy()
    env['XDG_RUNTIME_DIR'] = f'/run/user/{os.getuid()}'
    env['DBUS_SESSION_BUS_ADDRESS'] = f"unix:path={env['XDG_RUNTIME_DIR']}/bus"
    subprocess.run(['/usr/bin/systemctl', '--user', 'daemon-reload'], env=env, check=True)
    subprocess.run(['/usr/bin/systemctl', '--user', 'enable', 'hq-desktop.service'], env=env, check=True)
    subprocess.run(['/usr/bin/update-desktop-database', str(home / '.local/share/applications')], check=True)
    subprocess.run([launcher, '--session-start'], env=env, check=True)
    print(json.dumps({'installed_files': len(records), 'desktop_service': 'enabled, not started',
                      'receipt': str(state / 'installation.json')}))


if __name__ == '__main__':
    main()

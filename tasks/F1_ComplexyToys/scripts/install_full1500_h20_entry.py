"""Add1500 to the existing Full entry; preserve all prior model configurations."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    base = Path('/home/user/Bruno/DAgger-runtime/f1-toys-20261016/full-h20')
    staging = base / 'deploy1500-20261021'
    profile = json.loads((staging / 'profile.json').read_text())
    assert profile['local_port'] == 8154 and profile['remote_port'] == 8139
    assert profile['model_id'] == 'f1-toys-eef-e312de43fa3875a8'
    entry = Path('/home/user/Bruno/DAgger/start_f1_toys_full_h20.sh')
    helper = base / 'ensure_full_h20_rtc_tunnel.py'
    registry = base / 'profiles.json'
    with open('/home/user/Bruno/F1/runtime/control.lock', 'a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert sha(entry) == '74e31663871219a117a96755234fa4b34f612de11f53e4df9a3c7b238e044c35'
        old_entry = entry.read_text()
        expected = old_entry.replace('# Independent H20 Full 625/1000 entry.', '# Independent H20 Full entry.')
        expected = expected.replace('First argument must be 625 or 1000', 'First argument must be 625, 1000 or 1500')
        expected = expected.replace('625|1000', '625|1000|1500').replace('Choose 625 or 1000', 'Choose 625, 1000 or 1500')
        assert (staging / 'start_f1_full_h20.sh').read_text() == expected
        new_helper = (staging / helper.name).read_text()
        assert new_helper == helper.read_text().replace('choices=("625", "1000")', 'choices=("625", "1000", "1500")')
        subprocess.run(['bash', '-n', str(staging / 'start_f1_full_h20.sh')], check=True)
        old_profiles = json.loads(registry.read_text())
        assert set(old_profiles) == {'625', '1000'}
        pixels = base / '1500'
        pixels.mkdir(exist_ok=False)
        for name, key in [('contract.json', 'contract_sha256'), ('processing.py', 'implementation_sha256')]:
            shutil.copy2(base / '1000' / name, pixels / name)
            assert sha(pixels / name) == profile['input_preprocessing'][key]
        parameter_root = base / 'startup-read-v2'
        cfg = json.loads((parameter_root / '1000/config.json').read_text())
        release = Path('/home/user/Bruno/DAgger/releases/20261021-f1-toys-rtc-v6-param-read-r2/DAgger')
        assert sha(release / 'F1_RELEASE_MANIFEST.json') == cfg['accepted_release_manifest_sha256']
        cfg['policy'].update(endpoint='http://127.0.0.1:8154', model_id=profile['model_id'], rtc_pixels_directory=str(pixels))
        target = parameter_root / '1500'
        target.mkdir(exist_ok=False)
        (target / 'config.json').write_text(json.dumps(cfg, indent=2) + '\n')
        old_profiles['1500'] = profile
        backups = staging / 'before'
        backups.mkdir(exist_ok=False)
        for dest, content in [(registry, json.dumps(old_profiles, indent=2) + '\n'),
                              (helper, new_helper), (entry, expected)]:
            shutil.copy2(dest, backups / dest.name)
            temp = dest.with_name(dest.name + '.full1500.tmp')
            with temp.open('x') as f:
                f.write(content)
            temp.chmod(dest.stat().st_mode & 0o777)
            os.replace(temp, dest)
        receipt = dict(profile=profile, entry=str(entry), entry_sha256=sha(entry),
            config=str(target / 'config.json'), config_sha256=sha(target / 'config.json'),
            release_sha256=cfg['accepted_release_manifest_sha256'], delay_steps=14, min_steps=16,
            services_stopped=False, robot_commands=0, ssh_permissions_changed=False,
            pending='SSH permitopen8139 approval, actual tunnel/replay/shadow')
        (staging / 'installation.json').write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt))


if __name__ == '__main__':
    main()

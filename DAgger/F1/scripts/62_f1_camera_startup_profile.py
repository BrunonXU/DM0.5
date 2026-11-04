#!/usr/bin/env python3
"""Bind an audited camera startup copy to the workbench profile, with CAS rollback.

Only two profile fields change. Never start/restart a process or alter the vendor
startup. The running workbench must be reloaded separately while it is idle; the
base-start service must not be restarted to apply camera configuration.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

BRUNO_ROOT = Path('/home/user/Bruno')
MACHINE_ID = '00000000000000000000000000000000'
FIELDS = {'startup_script', 'startup_script_sha256'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def owned_path(value, *, exists=True):
    path = Path(value).absolute()
    resolved = path.resolve(strict=exists)
    if BRUNO_ROOT.resolve() not in resolved.parents or path != resolved:
        raise ValueError('Expected a canonical non-symlink path under Bruno: ' + str(path))
    return path


def atomic_write(path, data):
    mode = path.stat().st_mode & 0o777
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as target:
            os.fchmod(target.fileno(), mode)
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def verify_startup(startup_dir):
    startup = owned_path(startup_dir)
    release = startup.parent
    manifest = json.loads((release/'F1_RELEASE_MANIFEST.json').read_text())
    if manifest.get('schema') != 'f1_dagger_release_v1' or manifest.get('robot') != 'F1-09':
        raise ValueError('Not an F1-09 source release')
    files = manifest['files']
    required = ['scripts/60_f1_camera_internal.sh', 'configs/f1_camera_fastdds_internal.xml',
                'startup/start_all.sh', 'startup/start_all.diff',
                'startup/profile_patch.json', 'startup/manifest.json']
    for name in required:
        path = owned_path(release/name)
        if digest(path.read_bytes()) != files.get(name):
            raise ValueError('Release dependency hash mismatch: ' + name)
    proposal = json.loads((startup/'profile_patch.json').read_text())
    generated = json.loads((startup/'manifest.json').read_text())
    if set(proposal) != FIELDS or proposal['startup_script'] != str(startup/'start_all.sh'):
        raise ValueError('Startup proposal changes unexpected fields or points to another release')
    if proposal['startup_script_sha256'] != digest((startup/'start_all.sh').read_bytes()):
        raise ValueError('Startup proposal hash mismatch')
    if generated['camera_wrapper'] != str(release/'scripts/60_f1_camera_internal.sh'):
        raise ValueError('Startup wrapper is not part of this release')
    original = Path(generated['source']['robot_path'])
    if digest(original.read_bytes()) != generated['source']['sha256']:
        raise ValueError('Live vendor startup differs from the reviewed source')
    return proposal


def apply(*, profile, startup_dir, backup_dir, expected_profile_sha256):
    profile = owned_path(profile)
    proposal = verify_startup(startup_dir)
    before = profile.read_bytes()
    if digest(before) != expected_profile_sha256:
        raise ValueError('Profile changed since inventory; refusing to overwrite it')
    current = json.loads(before)
    if current.get('expected_machine_id') != MACHINE_ID:
        raise ValueError('Profile is not bound to F1-09')
    if all(current.get(k) == v for k, v in proposal.items()):
        return {'changed': False, 'profile': str(profile), 'profile_sha256': digest(before)}
    changed = dict(current)
    changed.update(proposal)
    after = (json.dumps(changed, indent=2, ensure_ascii=False) + '\n').encode()
    backup = owned_path(backup_dir, exists=False)
    backup.mkdir(parents=True, exist_ok=False)
    original = backup/'profile.before.json'
    with original.open('xb') as target:
        os.fchmod(target.fileno(), 0o600)
        target.write(before)
    record = {'schema': 'f1_camera_startup_profile_backup_v1', 'profile': str(profile),
              'before_sha256': digest(before), 'after_sha256': digest(after),
              'changed_fields': sorted(FIELDS), 'startup': proposal,
              'processes_restarted': False, 'workbench_reload_required': True}
    (backup/'manifest.json').write_text(json.dumps(record, indent=2) + '\n')
    if digest(profile.read_bytes()) != expected_profile_sha256:
        raise ValueError('Profile changed during preparation; backup retained, profile untouched')
    atomic_write(profile, after)
    return dict(record, changed=True, backup=str(backup))


def rollback(*, backup_dir):
    backup = owned_path(backup_dir)
    record = json.loads((backup/'manifest.json').read_text())
    if record.get('schema') != 'f1_camera_startup_profile_backup_v1':
        raise ValueError('Unexpected backup schema')
    profile = owned_path(record['profile'])
    before = (backup/'profile.before.json').read_bytes()
    if digest(before) != record['before_sha256']:
        raise ValueError('Backup profile hash mismatch')
    present = digest(profile.read_bytes())
    if present == record['before_sha256']:
        return {'changed': False, 'profile': str(profile), 'already_rolled_back': True}
    if present != record['after_sha256']:
        raise ValueError('Profile was edited after installation; refusing destructive rollback')
    atomic_write(profile, before)
    return {'changed': True, 'profile': str(profile), 'profile_sha256': digest(before),
            'processes_restarted': False, 'workbench_reload_required': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    install = sub.add_parser('apply')
    install.add_argument('--profile', required=True)
    install.add_argument('--startup-dir', required=True)
    install.add_argument('--backup-dir', required=True)
    install.add_argument('--expected-profile-sha256', required=True)
    undo = sub.add_parser('rollback')
    undo.add_argument('--backup-dir', required=True)
    args = vars(parser.parse_args())
    if Path('/etc/machine-id').read_text().strip() != MACHINE_ID:
        parser.error('This installer is bound to F1-09')
    function = apply if args.pop('command') == 'apply' else rollback
    try:
        result = function(**args)
    except (OSError, KeyError, ValueError) as exc:
        parser.exit(1, str(exc) + '\n')
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()

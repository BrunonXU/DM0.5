#!/usr/bin/env python3
"""Prepare or upload a versioned standalone F1-infra release; never start services."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import subprocess
import tarfile
import time

try:
    from .package_shared import ROOT, selected_files, source_bytes
except ImportError:
    from package_shared import ROOT, selected_files, source_bytes

DEFAULT_TARGET = '/home/user/Bruno/F1-infra'


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


class Remote:
    def __init__(self, host, ssh_config=None):
        if not host or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', host):
            raise ValueError('--host must be an explicit SSH hostname, address or alias')
        self.host = host
        self.options = ['-F', str(Path(ssh_config).expanduser().resolve())] if ssh_config else []

    def python(self, code):
        return run(['ssh', *self.options, self.host, 'python3 -'],
                   input=code, capture_output=True, text=True).stdout

    def copy(self, source, destination):
        run(['scp', *self.options, str(source), self.host + ':' + destination],
            stdout=subprocess.DEVNULL)

    def backup(self, source, destination):
        with destination.open('wb') as output:
            run(['ssh', *self.options, self.host,
                 'tar -czf - -C ' + shlex.quote(source) + ' .'], stdout=output)


def validate_target(target):
    path = PurePosixPath(target)
    if (not target.startswith('/') or '..' in path.parts or len(path.parts) < 4
            or not re.fullmatch(r'/[A-Za-z0-9_./-]+', target)
            or str(path) != target):
        raise ValueError('--target must be a dedicated absolute deployment directory')
    return target


def prepare_release(release_id, root=None):
    root = Path(root or ROOT).resolve()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', release_id):
        raise ValueError('Invalid release id')
    for required in ('README.md', 'AGENTS.md', 'server.py'):
        if not (root / required).is_file():
            raise ValueError(f'Missing project file: {required}')
    output = root / 'results/deployments' / release_id
    output.mkdir(parents=True, exist_ok=False)
    files = []
    archive = output / 'payload.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for path in selected_files(root, include_local_config=True):
            data = source_bytes(path)
            relative = 'F1-infra/' + path.relative_to(root).as_posix()
            mode = stat.S_IMODE(path.stat().st_mode)
            files.append({'path': relative, 'size': len(data),
                          'sha256': hashlib.sha256(data).hexdigest(), 'mode': oct(mode)})
            info = tarfile.TarInfo('workspace/' + relative)
            info.size, info.mode, info.mtime = len(data), mode, int(path.stat().st_mtime)
            tar.addfile(info, io.BytesIO(data))
        manifest = {'schema_version': 2, 'release_id': release_id, 'task': 'F1-infra',
                    'project_path': 'workspace/F1-infra', 'created_unix': time.time(),
                    'files': files,
                    'scope': 'standalone_source_and_config_only; no_service_start_or_motion'}
        manifest_bytes = (json.dumps(manifest, indent=2) + '\n').encode()
        (output / 'manifest.json').write_bytes(manifest_bytes)
        info = tarfile.TarInfo('manifest.json')
        info.size, info.mode = len(manifest_bytes), 0o644
        tar.addfile(info, io.BytesIO(manifest_bytes))
    with tarfile.open(archive) as tar:
        for entry in files:
            member = tar.getmember('workspace/' + entry['path'])
            data = tar.extractfile(member).read()
            if len(data) != entry['size'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
                raise ValueError('Release verification failed: ' + entry['path'])
            if stat.S_IMODE(member.mode) != int(entry['mode'], 8):
                raise ValueError('Release mode mismatch: ' + entry['path'])
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(archive.suffix + '.sha256').write_text(checksum + '  ' + archive.name + '\n')
    return output, archive, checksum


REMOTE_HELPERS = r'''
import hashlib,json,os,pathlib,tarfile

def require(condition, message):
    if not condition: raise RuntimeError(message)

def current_path(base):
    current = base/'current'
    require(not current.exists() or current.is_symlink(), 'current must be a release symlink')
    require(not current.is_symlink() or current.exists(), 'broken current symlink refused')
    if not current.exists(): return None
    actual = current.resolve()
    require(actual.is_relative_to(base/'releases'), 'current escaped releases')
    return str(actual)

def prior_manifest(base, current):
    # Old releases used workspace/F1/F1-4242; new ones use workspace/F1-infra.
    # Find the containing release rather than assuming a fixed parent depth.
    releases = (base/'releases').resolve()
    require(current.is_relative_to(releases), 'current escaped releases')
    for parent in (current, *current.parents):
        if parent == releases: break
        candidate = parent/'manifest.json'
        if candidate.is_file():
            require(candidate.resolve().is_relative_to(releases), 'manifest escaped releases')
            return json.loads(candidate.read_text())
    raise RuntimeError('prior release manifest missing')

def require_inactive(base, current):
    roots = [base/'current', current]
    for process in pathlib.Path('/proc').iterdir():
        if not process.name.isdigit() or process.name == str(os.getpid()): continue
        try:
            argv = (process/'cmdline').read_bytes().split(b'\0')
            args = [value.decode(errors='replace') for value in argv if value]
            # Both absolute launches and `python server.py` from the release.
            cwd = (process/'cwd').resolve(strict=True)
        except (OSError, PermissionError):
            continue
        active = any(cwd.is_relative_to(root.resolve()) for root in roots)
        for value in args:
            if value.startswith('/'):
                active = active or any(pathlib.Path(value).is_relative_to(root) for root in roots)
        active = active or 'pico_upper.supervisor' in args
        require(not active, 'task running; no activation permitted (pid '+process.name+')')
'''


def inspect_script(target):
    return REMOTE_HELPERS + f'''
base=pathlib.Path({target!r})
require(base.resolve()==base, 'symlink target refused')
print(json.dumps({{'exists':base.exists(),'entries':[p.name for p in base.iterdir()] if base.exists() else [],'current':current_path(base)}}))
'''


def audit_script(target, before, stage_only):
    return REMOTE_HELPERS + f'''
base=pathlib.Path({target!r}); current=pathlib.Path({before['current']!r})
require(base.resolve()==base, 'symlink target refused')
require(current_path(base)==str(current), 'current changed during deployment')
if not {stage_only!r}: require_inactive(base,current)
print(json.dumps({{'old_current':str(current),'prior_manifest':prior_manifest(base,current)}}))
'''


def activate_script(target, release_id, remote_archive, checksum, before, stage_only):
    return REMOTE_HELPERS + f'''
base=pathlib.Path({target!r}); release=base/'releases'/{release_id!r}
require(base.resolve()==base and not release.exists(), 'existing or symlink release refused')
require((base/'releases').resolve()==base/'releases', 'symlink releases directory refused')
require(current_path(base)=={before['current']!r}, 'current changed during upload')
archive=pathlib.Path({remote_archive!r})
require(archive.resolve()==archive, 'symlink archive refused')
require(hashlib.sha256(archive.read_bytes()).hexdigest()=={checksum!r}, 'upload hash mismatch')
with tarfile.open(archive) as tar:
    members=tar.getmembers(); names=[m.name for m in members]
    require(len(names)==len(set(names)), 'duplicate archive entries')
    for member in members:
        require(member.isfile(), 'non-regular archive member')
        require('..' not in pathlib.PurePosixPath(member.name).parts, 'archive path escaped release')
        require(str(pathlib.PurePosixPath(member.name))==member.name, 'non-canonical archive path')
        require((release/member.name).resolve().is_relative_to(release), 'archive path escaped release')
        require(member.name=='manifest.json' or member.name.startswith('workspace/F1-infra/'), 'unexpected archive scope')
    manifest=json.load(tar.extractfile('manifest.json'))
    expected={{'manifest.json'}} | {{'workspace/'+f['path'] for f in manifest['files']}}
    require(set(names)==expected, 'manifest file list mismatch')
    release.mkdir(parents=True)
    for member in members:
        destination=release/member.name
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_bytes(tar.extractfile(member).read())
        destination.chmod(member.mode & 0o777)
for entry in manifest['files']:
    path=release/'workspace'/entry['path']
    require(path.stat().st_size==entry['size'], 'file size mismatch')
    require(hashlib.sha256(path.read_bytes()).hexdigest()==entry['sha256'], 'file hash mismatch: '+entry['path'])
    require((path.stat().st_mode & 0o777)==(int(entry['mode'],8) & 0o777), 'file mode mismatch')
require((release/'workspace/F1-infra/server.py').is_file(), 'project entry point missing')
require(current_path(base)=={before['current']!r}, 'current changed during verification')
if not {stage_only!r}:
    if {before['current']!r}: require_inactive(base,pathlib.Path({before['current']!r}))
    link=base/('.current-'+{release_id!r})
    require(not link.exists() and not link.is_symlink(), 'temporary current link already exists')
    os.symlink(release/'workspace/F1-infra',link)
    os.replace(link,base/'current')
print(json.dumps({{'verified_files':len(manifest['files']),'staged_only':{stage_only!r},'release':str(release),'current':current_path(base),'commands_sent':0}}))
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', required=True)
    parser.add_argument('--host', help='Explicit SSH host, address or configured alias')
    parser.add_argument('--ssh-config', type=Path, help='Optional external SSH configuration')
    parser.add_argument('--target', default=DEFAULT_TARGET)
    parser.add_argument('--prepare-only', '--pack', dest='prepare_only', action='store_true',
                        help='Build and verify locally; do not connect to any host')
    parser.add_argument('--upgrade-inactive', action='store_true')
    parser.add_argument('--stage-only', action='store_true')
    args = parser.parse_args(argv)
    if not args.prepare_only and not args.host:
        parser.error('--host is required for upload; use --prepare-only for local packaging')
    target = validate_target(args.target)
    remote = None if args.prepare_only else Remote(args.host, args.ssh_config)
    output, archive, checksum = prepare_release(args.release)
    if args.prepare_only:
        print(json.dumps({'archive': str(archive), 'sha256': checksum, 'prepared_only': True}))
        return
    before = json.loads(remote.python(inspect_script(target)))
    (output / 'before.json').write_text(json.dumps(before, indent=2) + '\n')
    if before['current']:
        if not args.upgrade_inactive and not args.stage_only:
            raise RuntimeError('Existing current: use --upgrade-inactive after stopping this application')
        audit = remote.python(audit_script(target, before, args.stage_only))
        (output / 'backup').mkdir()
        (output / 'backup/audit.json').write_text(audit)
        backup = output / 'backup/previous_actual.tar.gz'
        remote.backup(before['current'], backup)
        backup.with_suffix(backup.suffix + '.sha256').write_text(
            hashlib.sha256(backup.read_bytes()).hexdigest() + '  ' + backup.name + '\n')
    else:
        allowed = {'runtime', 'incoming', 'releases', 'dev'}
        if before['exists'] and set(before['entries']) - allowed:
            raise RuntimeError('Unrecognized existing target; existing entry points are protected')
        (output / 'backup').mkdir()
        (output / 'backup/README.txt').write_text('No prior current deployment. Existing runtime preserved.\n')
    remote_archive = target + '/incoming/' + args.release + '.tar.gz'
    remote.python(REMOTE_HELPERS + f'''
base=pathlib.Path({target!r}); incoming=base/'incoming'
require(base.resolve()==base and incoming.resolve()==incoming, 'symlink target refused')
require((base/'releases').resolve()==base/'releases', 'symlink releases directory refused')
require(current_path(base)=={before['current']!r}, 'current changed before upload')
require(not pathlib.Path({remote_archive!r}).exists() and not pathlib.Path({remote_archive!r}).is_symlink(), 'incoming release already exists')
incoming.mkdir(parents=True,exist_ok=True)
''')
    remote.copy(archive, remote_archive)
    result = remote.python(activate_script(target, args.release, remote_archive, checksum,
                                          before, args.stage_only))
    (output / 'verification.json').write_text(result)
    print(result)


if __name__ == '__main__':
    main()

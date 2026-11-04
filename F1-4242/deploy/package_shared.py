#!/usr/bin/env python3
"""Package this standalone project's source, assets and documentation locally."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_ROOT = 'F1-infra'
# An explicit boundary: never walk a parent, sibling, connection workspace, or
# arbitrary new top-level directory. Extracted packages use the same boundary.
ROOT_FILES = (
    'README.md', 'AGENTS.md', '.gitignore', 'LICENSE', 'LICENSE.md',
    'requirements.txt', 'pyproject.toml', 'run.sh', 'setup.sh',
    'core.py', 'data_service.py', 'data_archives.py', 'data_rlt_assessment.py', 'data_annotations.py', 'data_organization.py', 'data_delete.py', 'health.py', 'rollout_catalog.py',
    'preview_service.py', 'recording_service.py', 'replay_service.py', 'replay_reports.py', 'server.py',
    'startup_service.py', 'teleop_service.py', 'hil_service.py', 'hil_contract.py', 'hil_media.py', 'hil_data.py', 'project_paths.py',
    'installation.env.example',
)
SOURCE_DIRS = (
    'configs', 'deploy', 'docs', 'integrations/pico_upper', 'recording',
    'robot', 'scripts', 'teleop', 'tests', 'web',
)
EXCLUDED = {
    '.git', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
    'node_modules', 'runtime', 'results', 'evidence', '.venv', 'venv',
    'build', 'install', 'log', 'logs', 'keys', '.ssh', 'credentials', 'connect',
}
SECRET_NAMES = {
    'access.token', 'control_code.json', 'credentials.json',
    'id_rsa', 'id_ed25519', 'id_ecdsa', 'id_dsa', '.env', '.netrc',
    'client_ssh_config', 'known_hosts', 'authorized_keys',
}
LOCAL_CONFIG_FILES = {'configs/robot.json', 'configs/installation.env', 'configs/site.json'}
EXCLUDED_SUFFIXES = {'.pyc', '.pyo', '.pem', '.key', '.so', '.o', '.zip', '.tar', '.gz'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def selected_files(root=None, include_local_config=False):
    root = Path(root or ROOT).resolve()
    paths = set()

    def consider(path):
        if path.is_symlink():
            raise ValueError(f'Symlink refused: {path}')
        if not path.is_file():
            return
        if not include_local_config and path.relative_to(root).as_posix() in LOCAL_CONFIG_FILES:
            return
        if path.name in SECRET_NAMES or path.name.startswith('.env.'):
            return
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            return
        if not path.resolve().is_relative_to(root):
            raise ValueError(f'Source escaped project: {path}')
        paths.add(path)

    for name in ROOT_FILES:
        consider(root / name)
    for name in SOURCE_DIRS:
        source = root / name
        if source.is_symlink():
            raise ValueError(f'Symlink refused: {source}')
        if not source.exists():
            continue
        if not source.resolve().is_relative_to(root):
            raise ValueError(f'Source escaped project: {source}')
        for current, dirs, files in os.walk(source, followlinks=False):
            parent = Path(current)
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDED)
            for directory in dirs:
                if (parent / directory).is_symlink():
                    raise ValueError(f'Symlink refused: {parent / directory}')
            for filename in sorted(files):
                consider(parent / filename)
    return sorted(paths)


def source_bytes(path):
    data = path.read_bytes()
    if re.search(rb'^-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----', data, re.MULTILINE):
        raise ValueError(f'Private key marker found: {path}')
    return data


def git_output(root, *args):
    try:
        result = subprocess.run(['git', '-C', str(root), *args], text=True,
                                capture_output=True, check=False)
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def create_package(output_dir, root=None, stamp=None):
    root = Path(root or ROOT).resolve()
    stamp = stamp or datetime.now().astimezone().strftime('%Y%m%d_%H%M%S')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', stamp):
        raise ValueError('Invalid package suffix')
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / ('F1-infra-' + stamp + '.zip')
    if archive.exists():
        raise FileExistsError(archive)
    for required in ('README.md', 'AGENTS.md', 'server.py'):
        if not (root / required).is_file():
            raise ValueError(f'Missing project file: {required}')
    manifest = {
        'package': ARCHIVE_ROOT, 'created_utc': datetime.now(timezone.utc).isoformat(),
        'source_kind': 'working_tree_including_uncommitted_and_untracked_files',
        'git_head': git_output(root, 'rev-parse', 'HEAD'),
        'git_status_scope': git_output(root, 'status', '--short', '--', '.'),
        'included_directories': SOURCE_DIRS, 'included_root_files': ROOT_FILES,
        'excluded_directories': sorted(EXCLUDED),
        'scope': 'standalone_project_source_assets_docs_tests; no_parent_workspace_runtime_or_credentials',
        'files': [],
    }
    handle, temporary = tempfile.mkstemp(prefix='.F1-infra-', suffix='.zip.tmp', dir=output_dir)
    os.close(handle)
    temporary = Path(temporary)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            def write(relative, data, mode=0o644):
                info = zipfile.ZipInfo(ARCHIVE_ROOT + '/' + relative,
                                      date_time=datetime.now().timetuple()[:6])
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, data)

            for path in selected_files(root):
                data = source_bytes(path)
                relative = path.relative_to(root).as_posix()
                mode = stat.S_IMODE(path.stat().st_mode)
                write(relative, data, mode)
                manifest['files'].append({'path': relative, 'bytes': len(data),
                                          'sha256': digest(data), 'mode': oct(mode)})
            manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()
            write('MANIFEST.json', manifest_bytes)
            sums = ''.join(f"{f['sha256']}  {f['path']}\n" for f in manifest['files'])
            sums += digest(manifest_bytes) + '  MANIFEST.json\n'
            write('SHA256SUMS', sums.encode())
        with zipfile.ZipFile(temporary) as z:
            if z.testzip() is not None:
                raise ValueError('ZIP CRC verification failed')
            for entry in manifest['files']:
                info = z.getinfo(ARCHIVE_ROOT + '/' + entry['path'])
                data = z.read(info)
                if len(data) != entry['bytes'] or digest(data) != entry['sha256']:
                    raise ValueError('ZIP content mismatch: ' + entry['path'])
                if stat.S_IMODE(info.external_attr >> 16) != int(entry['mode'], 8):
                    raise ValueError('ZIP mode mismatch: ' + entry['path'])
        # Do not silently replace another package that appeared while building.
        os.link(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)
    checksum = digest(archive.read_bytes())
    archive.with_suffix('.zip.sha256').write_text(checksum + '  ' + archive.name + '\n')
    return {'archive': str(archive.resolve()), 'files': len(manifest['files']),
            'bytes': archive.stat().st_size, 'sha256': checksum,
            'crc_content_hashes_and_modes_verified': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'results/share_packages')
    args = parser.parse_args()
    print(json.dumps(create_package(args.output_dir), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Build a reviewed public source ZIP without modifying the input or live install.

Input code is read only. Public Markdown must come from --docs-dir; internal
documentation is never copied implicitly. Privacy findings fail the build and
are written only to a separately requested audit report. No redaction changes
are made to control values, bindings, source text, or binary assets.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_ROOT = 'F1-infra'
ROOT_FILES = {
    'LICENSE', 'LICENSE.md', 'requirements.txt', 'pyproject.toml',
    'run.sh', 'setup.sh', 'core.py', 'data_service.py', 'data_annotations.py', 'data_organization.py',
    'data_delete.py', 'health.py', 'rollout_catalog.py', 'preview_service.py',
    'recording_service.py', 'replay_service.py', 'replay_reports.py', 'server.py',
    'startup_service.py', 'teleop_service.py', 'hil_service.py', 'hil_contract.py',
    'hil_media.py', 'hil_data.py', 'project_paths.py',
}
CONFIG_FILES = {
    'installation.env.example', 'robot.example.json', 'hil.example.json',
    'recording.unified.example.json', 'storage.example.json',
    'model_mapping.json', 'rollout_models.json',
}
DEPLOY_FILES = {
    'initialize.py', 'install_service.py', 'systemd/f1-console-4242.service',
    'f1-conversion-scan.service', 'f1-conversion-scan.timer',
}
SCRIPT_FILES = {
    'session_preview_worker.py', 'build_mcap_previews.py', 'replay_episode.py',
}
PICO_FILES = {
    'start.sh', 'stop.sh', 'check.sh', 'VERSION', 'UPSTREAM.json',
    'RELEASE_FILES.json', 'scripts/env.sh', 'scripts/wait_for_xr.py',
}
BLOCKED_PARTS = {
    '.git', '.ssh', '.env', '__pycache__', '.pytest_cache', '.mypy_cache',
    '.ruff_cache', 'tests', 'test', 'results', 'runtime', 'logs', 'log',
    'history', 'incidents', 'review', 'evidence', 'node_modules', '.venv',
    'venv', 'build', 'install', 'credentials', 'keys', 'backups', 'backup',
}
BLOCKED_NAMES = {
    'AGENTS.md', 'STATUS.md', 'access.token', 'control_code.json',
    'credentials.json', 'id_rsa', 'id_ed25519', 'known_hosts', 'authorized_keys',
}
CODE_SUFFIXES = {'.py', '.sh', '.json', '.yaml', '.yml', '.txt', '.cpp', '.c', '.h', '.hpp'}
ASSET_SUFFIXES = {'.stl', '.urdf', '.obj', '.mtl', '.png', '.svg', '.jpg', '.jpeg', '.webp', '.wav', '.mp3', '.ogg', '.woff', '.woff2', '.ttf'}
WEB_SUFFIXES = ASSET_SUFFIXES | {'.js', '.css', '.html', '.json', '.txt', '.ico'}
REQUIRED_FILES = {'README.md', 'server.py', 'run.sh', 'setup.sh', 'deploy/initialize.py', 'configs/robot.example.json'}
PRIVATE_PATTERNS = (
    ('private-key', rb'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----'),
    ('personal-account', rb'(?i)\b(?:private_operator_a|private_operator_b|000000001|000000002)\b'),
    ('personal-name', rb'(?i)\bBruno(?:nXU)?\b'),
    ('personal-home', rb'/home/(?!user(?:/|\b)|robot(?:/|\b)|<)[A-Za-z0-9_.-]+/'),
    ('personal-windows-home', rb'(?i)[A-Z]:[\\/]Users[\\/][^\s/\\"\']+'),
    ('site-ip', rb'(?<![\d.])(?:192\.0\.2\.3|192\.0\.2\.2|192\.0\.2\.1|192\.0\.2\.5|192\.0\.2\.11)(?![\d.])'),
    ('machine-id', rb'00000000000000000000000000000000'),
    ('credential', rb'(?i)(?:gh[pousr]_[A-Za-z0-9]{24,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{32,})'),
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_relative(name):
    """Reject ZIP aliases, Windows paths, traversal and noncanonical names."""
    if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
        raise ValueError('Unsafe archive path')
    path = PurePosixPath(name)
    if path.is_absolute() or ':' in name or any(p in ('', '.', '..') for p in name.split('/')):
        raise ValueError('Unsafe archive path: ' + name)
    return path.as_posix()


def is_license(name):
    return bool(re.fullmatch(r'(?:LICENSE|COPYING|NOTICE)(?:\.[A-Za-z0-9_-]+)?', name, re.I))


def selected(relative):
    parts = PurePosixPath(relative).parts
    if any(p.lower() in BLOCKED_PARTS for p in parts) or parts[-1].lower() in {p.lower() for p in BLOCKED_NAMES}:
        return False
    if any(p.startswith('.') for p in parts):
        return False
    suffix = PurePosixPath(relative).suffix.lower()
    if relative in ROOT_FILES:
        return True
    if parts[0] == 'configs':
        return '/'.join(parts[1:]) in CONFIG_FILES
    if parts[0] == 'deploy':
        return '/'.join(parts[1:]) in DEPLOY_FILES
    if parts[0] == 'scripts':
        return '/'.join(parts[1:]) in SCRIPT_FILES
    if parts[0] in {'recording', 'robot', 'teleop'}:
        return suffix in CODE_SUFFIXES or is_license(parts[-1])
    if parts[0] == 'web':
        return suffix in WEB_SUFFIXES or is_license(parts[-1])
    if parts[:2] == ('integrations', 'pico_upper'):
        sub = '/'.join(parts[2:])
        if sub in PICO_FILES or is_license(parts[-1]):
            return True
        if len(parts) > 3 and parts[2] in {'src', 'config', 'assets', 'vendor', 'licenses'}:
            return suffix in CODE_SUFFIXES | ASSET_SUFFIXES or is_license(parts[-1])
    return False


def read_directory(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Source directory must be a real directory')
    files, omitted = {}, []
    for current, dirs, names in os.walk(root, followlinks=False):
        parent = Path(current)
        dirs[:] = sorted(d for d in dirs if d.lower() not in BLOCKED_PARTS and not d.startswith('.'))
        for name in dirs:
            if (parent / name).is_symlink():
                raise ValueError('Symlink directory refused: ' + str((parent / name).relative_to(root)))
        for name in sorted(names):
            path = parent / name
            relative = safe_relative(path.relative_to(root).as_posix())
            if not selected(relative):
                omitted.append(relative)
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError('Nonregular source refused: ' + relative)
            files[relative] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode) & 0o777)
    return files, sorted(omitted)


def read_zip(source):
    """Read selected entries in memory; never use extractall on an input ZIP."""
    files, omitted, seen = {}, [], set()
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if len(entries) > 20000 or sum(e.file_size for e in entries) > 2 * 1024 ** 3:
            raise ValueError('Input archive exceeds source package limits')
        for entry in entries:
            name = safe_relative(entry.filename[:-1] if entry.is_dir() else entry.filename)
            if not name.startswith(ARCHIVE_ROOT + '/') and name != ARCHIVE_ROOT:
                raise ValueError('Input ZIP must have one F1-infra/ root')
            if name.casefold() in seen:
                raise ValueError('Duplicate archive path: ' + name)
            seen.add(name.casefold())
            mode = entry.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (kind == stat.S_IFDIR and not entry.is_dir()):
                raise ValueError('Nonregular archive member refused: ' + name)
            if entry.is_dir():
                continue
            relative = name[len(ARCHIVE_ROOT) + 1:]
            if not selected(relative):
                omitted.append(relative)
                continue
            if entry.file_size > 250 * 1024 ** 2:
                raise ValueError('Input file exceeds source package limit: ' + relative)
            files[relative] = (archive.read(entry), stat.S_IMODE(mode) & 0o777 or 0o644)
        if archive.testzip() is not None:
            raise ValueError('Input ZIP CRC failed')
    return files, sorted(omitted)


def overlay_docs(files, docs_dir):
    root = Path(docs_dir)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Public docs directory must be a real directory')
    for current, dirs, names in os.walk(root, followlinks=False):
        parent = Path(current)
        for name in dirs:
            if (parent / name).is_symlink():
                raise ValueError('Symlink in public docs')
        for name in sorted(names):
            path = parent / name
            relative = safe_relative(path.relative_to(root).as_posix())
            if path.is_symlink() or not path.is_file():
                raise ValueError('Nonregular public doc refused: ' + relative)
            if path.suffix.lower() != '.md':
                raise ValueError('Public docs overlay only accepts Markdown: ' + relative)
            if any(p.lower() in BLOCKED_PARTS for p in path.relative_to(root).parts) or name.lower() in {p.lower() for p in BLOCKED_NAMES}:
                raise ValueError('Internal document refused: ' + relative)
            if relative != 'README.md' and not relative.startswith('docs/'):
                raise ValueError('Public docs must be README.md or docs/*.md: ' + relative)
            files[relative] = (path.read_bytes(), 0o644)


def privacy_findings(files, deny_text=()):
    patterns = [(name, re.compile(pattern)) for name, pattern in PRIVATE_PATTERNS]
    patterns += [('extra-deny-text-' + str(i + 1), re.compile(re.escape(text.encode()))) for i, text in enumerate(deny_text)]
    findings = []
    for relative, (data, _) in sorted(files.items()):
        for category, pattern in patterns:
            for match in pattern.finditer(data):
                findings.append({'path': relative, 'category': category, 'line': data.count(b'\n', 0, match.start()) + 1})
    return findings


def write_new(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(data)


def verify_archive(archive_path, extract_dir=None):
    """Verify exact membership, hashes and modes; optionally extract to a NEW dir."""
    with zipfile.ZipFile(archive_path) as archive:
        manifest_bytes = archive.read(ARCHIVE_ROOT + '/MANIFEST.json')
        manifest = json.loads(manifest_bytes)
        expected = {ARCHIVE_ROOT + '/' + f['path'] for f in manifest['files']}
        expected |= {ARCHIVE_ROOT + '/MANIFEST.json', ARCHIVE_ROOT + '/SHA256SUMS'}
        if set(archive.namelist()) != expected or len(archive.namelist()) != len(expected) or archive.testzip() is not None:
            raise ValueError('Output ZIP membership or CRC mismatch')
        values = {}
        for entry in manifest['files']:
            relative = safe_relative(entry['path'])
            info = archive.getinfo(ARCHIVE_ROOT + '/' + relative)
            data = archive.read(info)
            if len(data) != entry['bytes'] or digest(data) != entry['sha256'] or stat.S_IMODE(info.external_attr >> 16) != int(entry['mode'], 8):
                raise ValueError('Output content or mode mismatch: ' + relative)
            values[relative] = (data, int(entry['mode'], 8))
        sums = ''.join(f"{f['sha256']}  {f['path']}\n" for f in manifest['files'])
        sums += digest(manifest_bytes) + '  MANIFEST.json\n'
        if archive.read(ARCHIVE_ROOT + '/SHA256SUMS') != sums.encode():
            raise ValueError('Output checksum list mismatch')
        values['MANIFEST.json'] = (manifest_bytes, 0o644)
        values['SHA256SUMS'] = (sums.encode(), 0o644)
    if extract_dir is not None:
        target = Path(extract_dir)
        target.mkdir(parents=True, exist_ok=False)
        for relative, (data, mode) in sorted(values.items()):
            path = target / ARCHIVE_ROOT / relative
            write_new(path, data)
            path.chmod(mode)
        for relative, (data, mode) in values.items():
            path = target / ARCHIVE_ROOT / relative
            if digest(path.read_bytes()) != digest(data) or stat.S_IMODE(path.stat().st_mode) != mode:
                raise ValueError('Extracted content or mode mismatch: ' + relative)
    return manifest


def create_package(*, source_dir=None, source_zip=None, docs_dir=None, output_dir=None,
                   version='V1.0', stamp='20261023', extract_dir=None, audit_report=None,
                   audit_only=False, deny_text=()):
    if (source_dir is None) == (source_zip is None):
        raise ValueError('Specify exactly one source directory or ZIP')
    if audit_report is not None and extract_dir is not None and Path(audit_report).resolve().is_relative_to(Path(extract_dir).resolve()):
        raise ValueError('Private audit report must stay outside the public preview directory')
    if not re.fullmatch(r'V[0-9]+(?:\.[0-9]+)*', version):
        raise ValueError('Invalid public version')
    date = datetime.strptime(stamp, '%Y%m%d')
    if date.year < 1980 or date.strftime('%Y%m%d') != stamp:
        raise ValueError('Invalid release date')
    if source_dir is not None:
        source = Path(source_dir).resolve()
        for target in (output_dir, extract_dir, audit_report):
            if target is not None and Path(target).resolve().is_relative_to(source):
                raise ValueError('Output must be outside the read-only source directory')
        files, omitted = read_directory(source_dir)
    else:
        files, omitted = read_zip(source_zip)
    overlay_docs(files, docs_dir or ROOT / 'deploy/public_docs')
    missing = REQUIRED_FILES - files.keys()
    if missing:
        raise ValueError('Missing required public files: ' + ', '.join(sorted(missing)))
    findings = privacy_findings(files, deny_text)
    audit = {'version': version, 'date': stamp, 'selected_files': sorted(files), 'omitted_files': omitted,
             'findings': findings, 'privacy_passed': not findings}
    if audit_report is not None:
        write_new(audit_report, (json.dumps(audit, ensure_ascii=False, indent=2) + '\n').encode())
    if audit_only:
        return {'files': len(files), 'findings': len(findings), 'privacy_passed': not findings, 'archive_created': False}
    if findings:
        raise ValueError(f'Privacy audit found {len(findings)} matches; inspect the private audit report and prepare a reviewed source')
    if output_dir is None:
        raise ValueError('--output-dir is required when building a package')
    output = Path(output_dir)
    archive_path = output / f'{ARCHIVE_ROOT}-{version}-{stamp}.zip'
    checksum_path = archive_path.with_suffix('.zip.sha256')
    for target in (archive_path, checksum_path, extract_dir):
        if target is not None and os.path.lexists(target):
            raise FileExistsError(target)
    manifest = {'package': ARCHIVE_ROOT, 'version': version, 'date': stamp, 'files': []}
    for relative, (data, mode) in sorted(files.items()):
        manifest['files'].append({'path': relative, 'bytes': len(data), 'sha256': digest(data), 'mode': oct(mode)})
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()
    files['MANIFEST.json'] = (manifest_bytes, 0o644)
    sums = ''.join(f"{f['sha256']}  {f['path']}\n" for f in manifest['files']) + digest(manifest_bytes) + '  MANIFEST.json\n'
    files['SHA256SUMS'] = (sums.encode(), 0o644)
    output.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix='.public-', suffix='.zip.tmp', dir=output)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for relative, (data, mode) in sorted(files.items()):
                info = zipfile.ZipInfo(ARCHIVE_ROOT + '/' + relative, date.timetuple()[:6])
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
        verify_archive(temporary)
        checksum = digest(temporary.read_bytes())
        os.link(temporary, archive_path)
    finally:
        temporary.unlink(missing_ok=True)
    write_new(checksum_path, (checksum + '  ' + archive_path.name + '\n').encode())
    if extract_dir is not None:
        verify_archive(archive_path, extract_dir)
    return {'archive': str(archive_path.resolve()), 'files': len(manifest['files']),
            'bytes': archive_path.stat().st_size, 'sha256': checksum, 'privacy_passed': True,
            'crc_content_hashes_and_modes_verified': True,
            'extracted': str(Path(extract_dir).resolve() / ARCHIVE_ROOT) if extract_dir is not None else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--source-zip', type=Path)
    source.add_argument('--source-dir', type=Path)
    parser.add_argument('--docs-dir', type=Path, default=ROOT / 'deploy/public_docs')
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--extract-dir', type=Path, help='New parent directory; extraction creates F1-infra/ inside it')
    parser.add_argument('--audit-report', type=Path, help='New private JSON file outside the public extracted tree')
    parser.add_argument('--audit-only', action='store_true')
    parser.add_argument('--deny-text', action='append', default=[])
    parser.add_argument('--version', default='V1.0')
    parser.add_argument('--stamp', default='20261023')
    args = parser.parse_args()
    try:
        result = create_package(**vars(args))
    except (ValueError, OSError, zipfile.BadZipFile) as error:
        parser.exit(2, str(error) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.audit_only and not result['privacy_passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()

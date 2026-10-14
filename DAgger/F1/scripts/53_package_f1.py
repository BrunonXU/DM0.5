#!/usr/bin/env python3
"""Package F1 with a pinned shared core in the established flat runtime layout."""
import argparse
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import tarfile

ROOT = Path(__file__).resolve().parents[1]
SUFFIXES = {'.py', '.sh', '.cmd', '.ps1', '.json', '.xml', '.txt', '.lock', '.md', '.toml', '.diff', '.example', '.wav'}


def source_files():
    # An unpacked release owns its pinned core and documentation. Neighboring
    # source trees are fallbacks only for a platform checkout without a bundle.
    core = ROOT / 'src/dagger_core'
    if not core.is_dir():
        core = ROOT.parent / 'shared/src/dagger_core'
    docs = ROOT / 'docs'
    if not docs.is_dir():
        docs = ROOT.parent / 'docs/F1'
    if not core.is_dir() or not docs.is_dir():
        raise FileNotFoundError('F1 shared source or documentation is missing')
    roots = {name: ROOT / name for name in ('src/f1dagger', 'windows_client', 'configs', 'scripts', 'startup', 'services')}
    roots.update({'src/dagger_core': core, 'docs': docs})
    files = {}
    for prefix, source in roots.items():
        if source.is_symlink():
            raise ValueError('symlink source directory refused: ' + str(source))
        for folder, dirs, names in os.walk(source, followlinks=False):
            parent = Path(folder)
            dirs[:] = sorted(d for d in dirs if d not in ('__pycache__', '.pytest_cache', '.git'))
            if any((parent / d).is_symlink() for d in dirs):
                raise ValueError('symlink source directory refused: ' + str(parent))
            for name in sorted(names):
                path = parent / name
                if path.suffix not in SUFFIXES:
                    continue
                if path.is_symlink():
                    raise ValueError('symlink file refused: ' + str(path))
                files[prefix + '/' + path.relative_to(source).as_posix()] = path
    for name in ('README.md', 'pyproject.toml'):
        files[name] = ROOT / name
    return files


def contents(name, source, destinations):
    data = source.read_bytes()
    if name == 'pyproject.toml':
        # This distribution carries its own pinned dagger_core package.
        text = data.decode().replace(', "dagger-core==0.2.0"', '')
        text = re.sub(r'^pythonpath = .*$', 'pythonpath = ["src"]', text, flags=re.MULTILINE)
        text = re.sub(r'^cache_dir = .*$', 'cache_dir = "artifacts/cache/pytest"', text, flags=re.MULTILINE)
        return text.encode()
    if name == 'README.md':
        data = data.replace(b'python3 F1/scripts/53_package_f1.py', b'python3 scripts/53_package_f1.py')
        data = data.replace(b'`../shared/`', b'`src/dagger_core/`')
    if source.suffix == '.md':
        def relocate(match):
            target = match.group(1)
            if '://' in target or target.startswith(('#', 'mailto:')):
                return match.group(0)
            location, marker, anchor = target.partition('#')
            destination = destinations.get((source.parent / location).resolve())
            if destination is None:
                return match.group(0)
            relative = os.path.relpath(destination, str(Path(name).parent)).replace(os.sep, '/')
            return '](' + relative + (marker + anchor if marker else '') + ')'
        return re.sub(r'\]\(([^)]+)\)', relocate, data.decode()).encode()
    return data


def package(output):
    files = source_files()
    destinations = {path.resolve(): name for name, path in files.items()}
    payloads = {name: contents(name, source, destinations) for name, source in sorted(files.items())}
    manifest = dict(schema='f1_dagger_release_v1', robot='F1-09', deployment_verified=False,
                    layout='platform_shared_v2',
                    files={name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()})
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as raw, tarfile.open(fileobj=raw, mode='w:gz') as archive:
        for name, data in payloads.items():
            info = tarfile.TarInfo('DAgger/' + name)
            info.size = len(data)
            info.mode = files[name].stat().st_mode & 0o777
            archive.addfile(info, BytesIO(data))
        data = json.dumps(manifest, indent=2).encode()
        info = tarfile.TarInfo('DAgger/F1_RELEASE_MANIFEST.json')
        info.size = len(data)
        archive.addfile(info, BytesIO(data))
    return dict(package=str(output), sha256=hashlib.sha256(output.read_bytes()).hexdigest(), files=len(files))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(package(args.output)))


if __name__ == '__main__':
    main()

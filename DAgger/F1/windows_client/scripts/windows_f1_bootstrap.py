"""Load one complete, verified operator package through an atomic pointer."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys


def resolve_package(root):
    pointer = json.loads((root/'current-package.json').read_text(encoding='utf-8-sig'))
    package = (root/pointer['directory']).resolve()
    if not package.is_relative_to((root/'packages').resolve()):
        raise ValueError('Operator package pointer escaped packages directory')
    raw = (package/'manifest.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != pointer['manifest_sha256']:
        raise ValueError('Operator manifest identity mismatch')
    for name, digest in json.loads(raw).items():
        path = (package/name).resolve()
        if not path.is_relative_to(package) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Operator package changed: '+name)
    return package


def main():
    root = Path(__file__).resolve().parents[1]
    package = resolve_package(root)
    os.environ['DAGGER_CLIENT_ROOT'] = str(root)
    sys.path.insert(0, str(package/'scripts'))
    sys.path.insert(0, str(package/'src'))
    runpy.run_path(str(package/'scripts/windows_f1_session.py'), run_name='__main__')


if __name__ == '__main__':
    main()

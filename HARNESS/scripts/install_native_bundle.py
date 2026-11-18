"""Install a verified development bundle into a new directory, never the existing venv."""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import tarfile


def extract_verified(archive, destination):
    """Check every archive member before extracting; reject undeclared files/links."""
    destination=Path(destination)
    if destination.exists():raise FileExistsError('new destination required')
    with tarfile.open(archive) as tar:
        members=tar.getmembers();names=[m.name for m in members]
        if len(names)!=len(set(names)) or 'manifest.json' not in names:
            raise ValueError('duplicate_or_missing_manifest')
        if any(not m.isfile() or Path(m.name).is_absolute() or '..' in Path(m.name).parts for m in members):
            raise ValueError('unsafe_archive_member')
        manifest=json.load(tar.extractfile('manifest.json'));hashes=manifest['files']
        if hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()!=manifest['bundle_id']:
            raise ValueError('manifest_identity_mismatch')
        if set(names)!=set(hashes)|{'manifest.json'}:raise ValueError('undeclared_archive_member')
        for name,expected in hashes.items():
            if hashlib.sha256(tar.extractfile(name).read()).hexdigest()!=expected:
                raise ValueError('bundle_file_integrity_failure')
        destination.mkdir(parents=True,exist_ok=False)
        tar.extractall(destination,filter='data')
    return manifest


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('archive',type=Path);p.add_argument('destination',type=Path);p.add_argument('--python',required=True)
    args=p.parse_args()
    manifest=extract_verified(args.archive,args.destination)
    subprocess.run([args.python,'-m','pip','install','--no-index','--no-deps','--no-compile',
        '--find-links',str(args.destination/'wheels'),'--target',str(args.destination/'deps'),
        '-r',str(args.destination/'native/requirements.lock')],check=True)
    # Exercise the native RTC child's actual import surface in this isolated
    # overlay. The base robot venv need not contain policy transport packages.
    subprocess.run([args.python,'-c',
        'import sys;sys.path.insert(0,sys.argv[1]);from websockets.sync.client import connect;assert callable(connect)',
        str(args.destination/'deps')],check=True)
    print(json.dumps(dict(installed=str(args.destination),bundle_id=manifest['bundle_id'],production_changed=False)))


if __name__=='__main__':main()

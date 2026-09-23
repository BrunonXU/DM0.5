"""F1 only. Verify an additive archive and the pinned baseline before installation.

Does not execute ROS, capture a position, stop services or publish motion commands.
Usage: python3 install_base_home_release.py /home/user/Bruno/.../release.tar.gz
"""
from pathlib import Path, PurePosixPath
import hashlib
import json
import os
import socket
import sys
import tarfile

ROOT=Path('/home/user/Bruno/F1/foam_sort_whole_body')
VERSION='20260917-base-home-v1'


def digest(data):return hashlib.sha256(data).hexdigest()


def safe(path):
    if path.is_symlink() or not path.resolve().is_relative_to(ROOT) or ROOT.resolve()!=ROOT:
        raise RuntimeError('Unsafe or symlinked destination: '+str(path))
    return path


def main():
    if socket.gethostname()!='ms-thor':raise RuntimeError('F1 ms-thor only')
    archive=safe(Path(sys.argv[1]))
    with tarfile.open(archive,'r:gz') as tar:
        members=tar.getmembers()
        names=[m.name for m in members]
        if len(names)!=len(set(names)):raise RuntimeError('Duplicate archive entries')
        if any(not m.isfile() or PurePosixPath(m.name).is_absolute() or '..' in PurePosixPath(m.name).parts for m in members):
            raise RuntimeError('Unexpected archive member')
        manifest=json.load(tar.extractfile('manifest.json'))
        if manifest['version']!=VERSION or manifest['root']!=str(ROOT):raise RuntimeError('Wrong release')
        files=manifest['files']
        if set(names)!=set(files)|{'manifest.json'}:raise RuntimeError('Manifest membership mismatch')
        for name, expected in manifest['baseline_expected'].items():
            if not name.startswith('releases/20260916-recorder-ready-v1/workspace/F1/foam_sort_whole_body/'):
                raise RuntimeError('Unexpected baseline path')
            path=safe(ROOT/name)
            if digest(path.read_bytes())!=expected:raise RuntimeError('Existing baseline changed: '+name)
        content={}
        for name,item in files.items():
            if name not in ('base_home.sh','pose_review.sh','replay_review.sh') and not name.startswith('releases/'+VERSION+'/'):
                raise RuntimeError('Unexpected target')
            data=tar.extractfile(name).read()
            if digest(data)!=item['sha256']:raise RuntimeError('Archive checksum mismatch')
            target=safe(ROOT/name)
            if target.exists() and target.read_bytes()!=data:raise RuntimeError('Refusing overwrite: '+name)
            content[name]=data
        # All baseline/checksum/overwrite checks complete before any writes.
        # New entry points installed last; old replay_v3/pose_v3/current never changed.
        for name in sorted(content, key=lambda n: (not n.startswith('releases/'), n)):
            target=safe(ROOT/name)
            target.parent.mkdir(parents=True,exist_ok=True)
            if not target.exists():
                with target.open('xb') as f:f.write(content[name])
                target.chmod(files[name]['mode'])
            if digest(target.read_bytes())!=files[name]['sha256']:raise RuntimeError('Installed checksum mismatch')
        record=ROOT/'releases'/VERSION/'deployment_manifest.json'
        if not record.exists():
            with record.open('x') as f:json.dump(manifest,f,indent=2)
        print(json.dumps(dict(installed=True,files_verified=len(content),version=VERSION,
                             old_entries_unchanged=True,motion_executed=False)))


if __name__=='__main__':main()

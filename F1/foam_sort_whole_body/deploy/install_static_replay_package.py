#!/usr/bin/env python3
"""F1: install a new hashed replay package without replacing existing content.

No ROS imports, processes, service changes or robot actions.
"""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile

ROOT=Path('/home/user/Bruno/F1/foam_sort_whole_body')

def digest(data):return hashlib.sha256(data).hexdigest()

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--archive',type=Path,required=True);ap.add_argument('--manifest',type=Path,required=True)
    args=ap.parse_args();m=json.loads(args.manifest.read_text());assert m['root']==str(ROOT)
    assert ROOT.resolve().is_relative_to(Path('/home/user/Bruno').resolve())
    expected=m['files'];contents={}
    with tarfile.open(args.archive,'r:gz') as archive:
        for member in archive.getmembers():
            name=member.name;p=Path(name)
            assert member.isfile() and not p.is_absolute() and '..' not in p.parts
            assert name in expected and name not in contents
            assert member.size==expected[name]['size_bytes']
            data=archive.extractfile(member).read();assert digest(data)==expected[name]['sha256']
            target=ROOT/name
            for parent in [target,*target.parents]:
                if parent==ROOT.parent:break
                assert not parent.is_symlink(),f'Symlink refused: {parent}'
            if target.exists():assert target.is_file() and digest(target.read_bytes())==expected[name]['sha256'],f'Existing different file refused: {target}'
            contents[name]=data
    assert set(contents)==set(expected)
    # All archive entries and existing destinations have been checked first.
    created=[]
    for name,data in contents.items():
        target=ROOT/name;target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():
            with target.open('xb') as f:f.write(data)
            target.chmod(0o755 if name.endswith('.sh') else 0o644);created.append(name)
        assert digest(target.read_bytes())==expected[name]['sha256']
    print(json.dumps(dict(version=m['version'],files_verified=len(expected),created=created,
                         existing_different_files_overwritten=False,robot_motion_sent=False),indent=2))

if __name__=='__main__':main()

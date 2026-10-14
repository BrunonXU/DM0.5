#!/usr/bin/env python3
"""Stage an immutable F1 source release in a NEW Bruno directory; never activate."""
import argparse
import base64
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile



def inspect_package(path):
    """Reject incomplete, altered or unbound archives before opening SSH."""
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        names = [item.name for item in members]
        if len(set(names)) != len(names):
            raise ValueError('duplicate archive paths')
        for item in members:
            relative = Path(item.name)
            if not item.isfile() or relative.is_absolute() or '..' in relative.parts or not item.name.startswith('DAgger/'):
                raise ValueError('invalid archive member: ' + item.name)
        manifest = json.load(archive.extractfile('DAgger/F1_RELEASE_MANIFEST.json'))
        if set(names) != {'DAgger/' + name for name in manifest['files']} | {'DAgger/F1_RELEASE_MANIFEST.json'}:
            raise ValueError('unbound archive files')
        for name, digest in manifest['files'].items():
            if hashlib.sha256(archive.extractfile('DAgger/' + name).read()).hexdigest() != digest:
                raise ValueError('file hash mismatch: ' + name)
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package',required=True,type=Path)
    parser.add_argument('--host',required=True)
    parser.add_argument('--ssh-config',required=True,type=Path)
    parser.add_argument('--target',required=True)
    parser.add_argument('--evidence',required=True,type=Path)
    args=parser.parse_args()
    if args.host.startswith('-') or not args.ssh_config.is_file():parser.error('explicit SSH host/config required')
    if args.evidence.exists():parser.error('evidence exists; inspect it before another stage operation')
    inspect_package(args.package)
    content=args.package.read_bytes()
    request=dict(target=args.target,data=base64.b64encode(content).decode(),sha256=hashlib.sha256(content).hexdigest())
    remote='request='+repr(request)+'\n'+r'''
import base64,hashlib,io,json,os,tarfile
from pathlib import Path
root=Path(request['target']).resolve()
if Path('/home/user/Bruno') not in root.parents:raise ValueError('target outside Bruno')
partial=root.with_name(root.name+'.partial')
if root.exists() or partial.exists():raise FileExistsError('existing release/partial; will not overwrite')
data=base64.b64decode(request['data'])
if hashlib.sha256(data).hexdigest()!=request['sha256']:raise ValueError('package hash mismatch')
with tarfile.open(fileobj=io.BytesIO(data)) as archive:
 members=archive.getmembers()
 names=[m.name for m in members]
 if len(set(names))!=len(names):raise ValueError('duplicate archive paths')
 for member in members:
  destination=(partial/member.name).resolve()
  if not member.isfile() or not member.name.startswith('DAgger/') or partial not in destination.parents:
   raise ValueError('invalid archive member: '+member.name)
 manifest=json.load(archive.extractfile('DAgger/F1_RELEASE_MANIFEST.json'))
 if set(names)!={'DAgger/'+n for n in manifest['files']}|{'DAgger/F1_RELEASE_MANIFEST.json'}:
  raise ValueError('unbound archive files')
 for name,digest in manifest['files'].items():
  if hashlib.sha256(archive.extractfile('DAgger/'+name).read()).hexdigest()!=digest:raise ValueError('file hash mismatch: '+name)
 root.parent.mkdir(parents=True,exist_ok=True);partial.mkdir()
 for member in members:
  destination=partial/member.name;destination.parent.mkdir(parents=True,exist_ok=True)
  with destination.open('xb') as stream:stream.write(archive.extractfile(member).read())
  destination.chmod(member.mode & 0o777)
 for name,digest in manifest['files'].items():
  if hashlib.sha256((partial/'DAgger'/name).read_bytes()).hexdigest()!=digest:raise ValueError('staged hash mismatch')
 os.rename(partial,root)
print(json.dumps(dict(release=str(root/'DAgger'),files=len(manifest['files']),package_sha256=request['sha256'],
 previous_release_at_target=False,old_files_overwritten=False,activated=False,processes_started=False)))
'''
    result=subprocess.run(['ssh','-F',str(args.ssh_config.resolve()),'-o','BatchMode=yes',args.host,'python3','-'],
                          input=remote,text=True,capture_output=True,timeout=30)
    if result.returncode:raise RuntimeError(result.stderr.strip())
    evidence=json.loads(result.stdout)
    evidence.update(checked_at_utc=datetime.now(timezone.utc).isoformat(),host=args.host)
    args.evidence.parent.mkdir(parents=True,exist_ok=True)
    with args.evidence.open('x') as stream:json.dump(evidence,stream,indent=2)
    print(json.dumps(evidence,indent=2))

if __name__=='__main__':main()

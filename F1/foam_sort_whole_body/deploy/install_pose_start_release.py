"""Install explicit reviewed start policy, backing up the one changed entry."""
from pathlib import Path,PurePosixPath
import json,hashlib,tarfile,sys,socket,os
ROOT=Path('/home/user/Bruno/F1/foam_sort_whole_body')
VERSION='20260917-pose-start-intersection-v1'
def sha(data):return hashlib.sha256(data).hexdigest()
def safe(p):
 if p.is_symlink() or ROOT.resolve()!=ROOT or not p.resolve().is_relative_to(ROOT):raise RuntimeError('unsafe path: '+str(p))
 return p

def main():
 if socket.gethostname()!='ms-thor':raise RuntimeError('wrong host')
 for p in Path('/proc').glob('[0-9]*/cmdline'):
  try:
   cmd=p.read_bytes().split(b'\0')
   if b'f1_replay.pose_cli' in cmd and (b'to-start' in cmd or b'restore' in cmd):raise RuntimeError('Pose action active; defer entry switch')
  except (FileNotFoundError,PermissionError):pass
 with tarfile.open(safe(Path(sys.argv[1])),'r:gz') as t:
  members=t.getmembers();names=[m.name for m in members]
  if len(names)!=len(set(names)) or any(not m.isfile() or PurePosixPath(m.name).is_absolute() or '..' in PurePosixPath(m.name).parts for m in members):raise RuntimeError('unsafe archive')
  manifest=json.load(t.extractfile('manifest.json'))
  if manifest['version']!=VERSION or manifest['root']!=str(ROOT):raise RuntimeError('wrong release')
  if set(names)!=set(manifest['files'])|{'manifest.json'}:raise RuntimeError('manifest mismatch')
  for name,expected in manifest['baseline_expected'].items():
   if name!='pose_review.sh' and not name.startswith('releases/'):raise RuntimeError('unexpected baseline')
   if sha(safe(ROOT/name).read_bytes())!=expected:raise RuntimeError('baseline changed: '+name)
  content={}
  for name,item in manifest['files'].items():
   if name!='pose_review.sh' and not name.startswith('releases/'+VERSION+'/'):raise RuntimeError('unexpected target')
   target=safe(ROOT/name);data=t.extractfile(name).read()
   if sha(data)!=item['sha256']:raise RuntimeError('checksum mismatch')
   if name!='pose_review.sh' and target.exists():raise RuntimeError('release exists; inspect before repeat')
   content[name]=data
  release=safe(ROOT/'releases'/VERSION)
  release.mkdir(exist_ok=False)
  backup=release/'backup';backup.mkdir()
  (backup/'pose_review.sh').write_bytes((ROOT/'pose_review.sh').read_bytes())
  for name,data in content.items():
   if name=='pose_review.sh':continue
   target=safe(ROOT/name);target.parent.mkdir(parents=True,exist_ok=True)
   with target.open('xb') as f:f.write(data)
   target.chmod(manifest['files'][name]['mode'])
   if sha(target.read_bytes())!=manifest['files'][name]['sha256']:raise RuntimeError('write verification failed')
  temp=ROOT/('.pose_review.'+str(os.getpid())+'.sh')
  with temp.open('xb') as f:f.write(content['pose_review.sh'])
  temp.chmod(0o755);temp.replace(ROOT/'pose_review.sh')
  (release/'deployment_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
  print(json.dumps(dict(installed=True,version=VERSION,files_verified=len(content),old_entry_backup=str(backup/'pose_review.sh'),motion_executed=False)))
if __name__=='__main__':main()

#!/usr/bin/env python3
"""Versioned upload of these two new F1 tools; no robot motion or services."""
import argparse,hashlib,json,os,shlex,subprocess,tarfile,time
from pathlib import Path
WORK=Path(__file__).resolve().parents[3]
SSH_CONFIG=WORK/'F1/connect/robots/f1-09/client_ssh_config'
def run(args,**kw):return subprocess.run(args,check=True,**kw)
def ssh(code):
    return run(['ssh','-F',str(SSH_CONFIG),'F1-09','python3 -c '+shlex.quote(code)],capture_output=True,text=True).stdout

def main():
    parser=argparse.ArgumentParser();parser.add_argument('task',choices=['F1-4242','pico_upper']);parser.add_argument('--release',required=True);parser.add_argument('--upgrade-inactive',action='store_true');args=parser.parse_args()
    if not args.release.replace('-','').replace('_','').isalnum():raise ValueError('invalid release id')
    task=args.task;source=WORK/'F1'/task;target='/home/user/Bruno/F1/'+task
    output=source/'results/deployments'/args.release;output.mkdir(parents=True,exist_ok=False)
    files=[]
    for p in sorted(source.rglob('*')):
        rel=p.relative_to(source)
        if any(x in ('runtime','results','__pycache__','.pytest_cache','.git','node_modules') for x in rel.parts):continue
        if p.is_symlink():raise ValueError('No symlinks in package: '+str(p))
        if not p.is_file():continue
        files.append({'path':str(p.relative_to(WORK)),'size':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
    manifest={'release_id':args.release,'task':task,'created_unix':time.time(),'files':files,'scope':'code_and_config_only; no_motion; no_recording_change'}
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    archive=output/'payload.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:
        for f in files:tar.add(WORK/f['path'],arcname='workspace/'+f['path'])
        tar.add(output/'manifest.json',arcname='manifest.json')
    # Refuse existing installations until a separate reviewed upgrade backs them up.
    before=json.loads(ssh(f'''import json,pathlib
p=pathlib.Path({target!r})
assert p.resolve()==p, 'symlink target refused'
print(json.dumps({{'exists':p.exists(),'entries':[x.name for x in p.iterdir()] if p.exists() else [],'current':str((p/'current').resolve()) if (p/'current').exists() else None}}))'''))
    (output/'before.json').write_text(json.dumps(before,indent=2)+'\n')
    if before['current']:
        if not args.upgrade_inactive:raise RuntimeError('Existing current: use --upgrade-inactive after ensuring this task is stopped')
        audit=ssh(f'''import json,pathlib
base=pathlib.Path({target!r}); current=(base/'current').resolve()
assert current.is_relative_to(base/'releases')
for p in pathlib.Path('/proc').iterdir():
 if not p.name.isdigit():continue
 try:args=(p/'cmdline').read_bytes().split(b"\\0")
 except OSError:continue
 assert b'pico_upper.supervisor' not in args and str(current/'server.py').encode() not in args,'task running; no activation permitted'
manifest=json.loads((current.parents[2]/'manifest.json').read_text())
print(json.dumps({{'old_current':str(current),'prior_manifest':manifest}}))''')
        (output/'backup').mkdir();(output/'backup/audit.json').write_text(audit)
        with (output/'backup/previous_actual.tar.gz').open('wb') as backup:
            run(['ssh','-F',str(SSH_CONFIG),'F1-09','tar -czf - -C '+shlex.quote(before['current'])+' .'],stdout=backup)
    else:
        if before['exists'] and any(x not in ('runtime','incoming','releases','dev') for x in before['entries']):raise RuntimeError('Unrecognized existing target')
        (output/'backup').mkdir();(output/'backup/README.txt').write_text('No prior current deployment. Runtime environment, if present, preserved.\n')
    remote_archive=target+'/incoming/'+args.release+'.tar.gz'
    ssh(f'''from pathlib import Path
p=Path({target!r})
assert p.resolve()==p
(p/'incoming').mkdir(parents=True,exist_ok=True)
assert not Path({remote_archive!r}).exists()''')
    run(['scp','-F',str(SSH_CONFIG),str(archive),'F1-09:'+remote_archive],stdout=subprocess.DEVNULL)
    result=ssh(f'''import hashlib,json,os,pathlib,tarfile
base=pathlib.Path({target!r}); release=base/'releases'/{args.release!r}
assert base.resolve()==base and not release.exists()
release.mkdir(parents=True)
with tarfile.open({remote_archive!r}) as tar:
 for m in tar.getmembers():
  assert m.isfile() and not m.issym() and not m.islnk()
  assert (release/m.name).resolve().is_relative_to(release.resolve())
  assert m.name=='manifest.json' or m.name.startswith('workspace/F1/{task}/')
 tar.extractall(release,filter='data')
manifest=json.loads((release/'manifest.json').read_text())
for f in manifest['files']:
 p=release/'workspace'/f['path']; assert p.stat().st_size==f['size']; assert hashlib.sha256(p.read_bytes()).hexdigest()==f['sha256'],f['path']
assert str((base/'current').resolve())=={before['current']!r} if {before['current'] is not None!r} else not (base/'current').exists()
link=base/('.current-'+{args.release!r});os.symlink(release/'workspace/F1/{task}',link);os.replace(link,base/'current')
print(json.dumps({{'verified_files':len(manifest['files']),'current':str((base/'current').resolve()),'commands_sent':0}}))''')
    (output/'verification.json').write_text(result);print(result)
if __name__=='__main__':main()

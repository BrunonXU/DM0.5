"""Stage save-button candidates and activate idle entries/static help only.

No model services, ROS sessions or robot motions are started. The installed
workbench backend and unrelated live web assets remain in place.
"""
import argparse
import difflib
import json
from pathlib import Path
import subprocess
import tarfile
import deploy_manual_recovery as base
from prepare_manual_finish import ROOT,OUT,STAGE,sha


ACTIVATE=base.COMMON.replace('manual-recovery-20261023','manual-save-20261023')+r'''
idle()
assembly=Path(p['assembly_destination']);verify(assembly,'assembly-manifest.json')
for item in p['packages'].values():assert verify(Path(item['destination']),'F1_RELEASE_MANIFEST.json')==item['manifest_sha256']
assert (data/'config.json').read_text()==p['manual_config']
manual=Path('/home/user/Bruno/DAgger/start_f1_hil_route.sh');registry=b/'runtime/hil-routes.json'
pointer=Path('/home/user/Bruno/DAgger-runtime/inference-current')
assert str(pointer.resolve())==p['old_assembly'] and str((b/'current').resolve())==p['old_web_current']
assert sha(manual)==p['manual_before'] and sha(registry)==p['registry_before']
for name,item in p['web'].items():assert sha(b/'dev/web'/name)==item['before'],name
backup=data/'activation-backup';assert not backup.exists()
with open('/home/user/Bruno/F1/runtime/control.lock','a+') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);idle(lock_held=True)
 assert str(pointer.resolve())==p['old_assembly'] and str((b/'current').resolve())==p['old_web_current']
 assert sha(manual)==p['manual_before'] and sha(registry)==p['registry_before']
 for name,item in p['web'].items():assert sha(b/'dev/web'/name)==item['before'],name
 backup.mkdir();shutil.copy2(manual,backup/'manual-entry.sh');shutil.copy2(registry,backup/'routes.json');shutil.copy2(b/'dev/sync.json',backup/'sync.json')
 (backup/'previous-assembly.txt').write_text(str(pointer.resolve()))
 for name in p['web']:shutil.copy2(b/'dev/web'/name,backup/name)
 def replace_text(target,text):
  temp=target.with_name(target.name+'.save-next');temp.write_text(text);temp.chmod(target.stat().st_mode&0o777);os.replace(temp,target)
 def replace_link(target,value):
  temp=target.with_name(target.name+'.save-next');temp.symlink_to(value);os.replace(temp,target)
 try:
  for name,item in p['web'].items():replace_text(b/'dev/web'/name,item['source'])
  sync=json.loads((b/'dev/sync.json').read_text());sync['revision']='20261023-manual-save-r1'
  for name,item in p['web'].items():sync['files'][name]=item['after']
  replace_text(b/'dev/sync.json',json.dumps(sync,ensure_ascii=False))
  routes=json.loads(registry.read_text())
  for route,hint in p['route_hints'].items():routes['routes'][route]['hint']=hint
  replace_text(registry,json.dumps(routes,ensure_ascii=False,indent=2)+'\n')
  replace_text(manual,p['manual_entry']);replace_link(pointer,assembly)
  ready=get('/api/hil/routes');assert all(x['ready'] for x in ready['routes'])
  for name,item in p['web'].items():assert hashlib.sha256(urlopen('http://127.0.0.1:4242/'+name).read()).hexdigest()==item['after']
  idle(lock_held=True)
 except BaseException:
  shutil.copy2(backup/'manual-entry.sh',manual);shutil.copy2(backup/'routes.json',registry);shutil.copy2(backup/'sync.json',b/'dev/sync.json')
  for name in p['web']:shutil.copy2(backup/name,b/'dev/web'/name)
  replace_link(pointer,p['old_assembly'])
  raise
receipt=dict(assembly=str(pointer.resolve()),manual=sha(manual),web_backend=str((b/'current').resolve()),
 web_revision='20261023-manual-save-r1',web_files={n:e['after'] for n,e in p['web'].items()},backup=str(backup),
 routes=ready,service_restarted=False,models_started=False,robot_commands=0,Y_preserved=True,windows_update_required=False)
(data/'activation.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2));print(json.dumps(receipt,ensure_ascii=False))
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['stage','activate']);parser.add_argument('--optimized',action='store_true');args=parser.parse_args()
    stage=OUT/'staging-optimized' if args.optimized else STAGE
    before=json.loads((OUT/'remote-before.json').read_text())
    if args.optimized:
        current=json.loads((OUT/'remote-after-concurrent-rollout.json').read_text())
        before.update(assembly=current['assembly'],web_current=current['web_current'])
        before['files'].update(current['files'])
    summary=json.loads((stage/'summary.json').read_text());catalog=json.loads((stage/'catalog.json').read_text())
    payload=dict(packages=summary['packages'],assembly_destination=catalog['robot_release'],manual_config=(stage/'manual-config.json').read_text(),manual_entry=(stage/'manual-entry.sh').read_text())
    if args.action=='stage':
        for item in payload['packages'].values():item['upload']=base.upload(Path(item['archive']))
        for name,folder in [('assembly',stage/'assembly'),('tests',ROOT/'DAgger')]:
            archive=stage/(name+'.tar.gz')
            with tarfile.open(archive,'w:gz') as tar:
                paths=folder.rglob('*') if name=='assembly' else [p for sub in ['F1/tests','G2/tests','shared/tests','G2/src'] for p in (folder/sub).rglob('*')]
                for path in sorted(paths):
                    if path.is_file() and '__pycache__' not in path.parts:tar.add(path,arcname=str(path.relative_to(folder)),recursive=False)
            payload[name+'_upload']=base.upload(archive);payload[name+'_sha256']=sha(archive)
        code=base.STAGE_REMOTE.replace('manual-recovery-20261023','manual-save-20261023')
        if args.optimized:
            # Reuse the already verified manual candidate and test fixture tree.
            # A different existing release is always rejected, never overwritten.
            code=code.replace(" extract(item['upload'],dest.parent,item['archive_sha256'])", " if not dest.exists():extract(item['upload'],dest.parent,item['archive_sha256'])")
            code=code.replace("data.mkdir(exist_ok=False)", "assert data.is_dir() and (data/'config.json').read_text()==p['manual_config'] and (data/'candidate-manual.sh').read_text()==p['manual_entry']")
            code=code.replace("extract(p['tests_upload'],data/'validation',p['tests_sha256'])", "assert (data/'validation/F1/tests/test_f1_episode_controls.py').is_file()")
            code=code.replace("(data/'stage.json')", "(data/'stage-optimized.json')")
        result=base.remote(code,payload)
    else:
        payload.update(old_assembly=before['assembly'],old_web_current=before['web_current'],manual_before=before['files']['/home/user/Bruno/DAgger/start_f1_hil_route.sh']['sha256'],registry_before=before['files']['/home/user/Bruno/F1/F1-4242/runtime/hil-routes.json']['sha256'])
        payload['web']={};web=stage/'web';web.mkdir(exist_ok=True)
        for name in ('hil.js','hil-instructions.js','hil-teaching.js','hil-workspace.js'):
            old=OUT/'source-before/F1-4242/web'/name;source=ROOT/'F1-4242/web'/name
            deployed=before['files']['/home/user/Bruno/F1/F1-4242/dev/web/'+name]
            target=web/name;target.write_text(deployed['text'])
            delta=''.join(difflib.unified_diff(old.read_text().splitlines(True),source.read_text().splitlines(True),fromfile='a/'+name,tofile='b/'+name))
            assert delta,name
            subprocess.run(['patch','--batch','--fuzz=0','-p1'],input=delta,cwd=web,text=True,check=True,capture_output=True)
            payload['web'][name]=dict(before=deployed['sha256'],after=sha(target),source=target.read_text())
        payload['route_hints']={
            'demonstration':'连续示教：START开条，RB控右臂；收尾由操作者决定。松RB并等夹爪动作完成后↑保存；Y可选，回位结束即可保存，不要求两秒保持。',
            'policy':'DAgger1K：START开始策略，再START人工接管。收尾由操作者决定；松RB并等夹爪动作完成后↑保存，Y可选，不要求回位或两秒保持。'}
        result=base.remote(ACTIVATE,payload)
    (OUT/(args.action+('-optimized' if args.optimized else '')+'-receipt.json')).write_text(result);print(result)


if __name__=='__main__':main()

"""Freeze only the operator-selected save condition and matching instructions."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

TASK=Path(__file__).resolve().parents[1]
ROOT=TASK.parents[1]
OUT=TASK/'artifacts/20261023-manual-finish'
STAGE=OUT/'staging'
PREVIOUS=TASK/'artifacts/20261023-manual-recovery/staging'


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def dump(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def optimized():
    """Rebase the already frozen save patch onto the installed optimized HIL."""
    stage=OUT/'staging-optimized'
    remote=json.loads((OUT/'remote-after-concurrent-rollout.json').read_text())
    component=json.loads((OUT/'optimized-components.json').read_text())
    prior_summary=json.loads((STAGE/'summary.json').read_text())
    assembly=TASK/'artifacts/20261023-rlinf-deploy/assembly'
    for name in ('catalog.json','assembly-manifest.json'):
        evidence=remote['files'][remote['assembly']+'/'+name]
        assert sha(assembly/name)==evidence['sha256'],name
        assert hashlib.sha256(evidence['text'].encode()).hexdigest()==evidence['sha256'],name
    original_assembly=json.loads((assembly/'assembly-manifest.json').read_text())
    for rel,digest in original_assembly['files'].items():
        assert sha(assembly/rel)==digest,rel
    catalog=json.loads((assembly/'catalog.json').read_text())
    default_hil=catalog['models'][catalog['default_hil']]['modes']['hil']
    assert default_hil['client_component']==dict(client=component['release'],
        manifest_sha256=component['manifest_sha256'])

    base=PREVIOUS/'hil/DAgger'
    base_manifest=json.loads((base/'F1_RELEASE_MANIFEST.json').read_text())
    manifest=component['manifest']
    assert manifest['parent_manifest_sha256']==sha(base/'F1_RELEASE_MANIFEST.json')
    assert manifest['files'].keys()==base_manifest['files'].keys()
    optimized_files={'src/f1dagger/rtc_pixels.py','src/f1dagger/rtc_policy.py'}
    assert set(component['files'])==optimized_files
    assert {name for name,digest in manifest['files'].items()
        if digest!=base_manifest['files'][name]}==optimized_files
    for name,text in component['files'].items():
        assert hashlib.sha256(text.encode()).hexdigest()==manifest['files'][name],name
    for rel,digest in base_manifest['files'].items():
        assert sha(base/rel)==digest,rel

    # Reuse the reviewed save artifacts, without incorporating later edits to
    # the development tree or rebuilding the unaffected manual release.
    saved=STAGE/'hil/DAgger'
    saved_manifest=json.loads((saved/'F1_RELEASE_MANIFEST.json').read_text())
    assert sha(saved/'F1_RELEASE_MANIFEST.json')==prior_summary['packages']['hil']['manifest_sha256']
    save_files={'src/f1dagger/'+name for name in
        ('episode_controls.py','rtc_hil_feedback.py','cli.py')}
    assert {name for name,digest in saved_manifest['files'].items()
        if digest!=base_manifest['files'][name]}==save_files
    stage.mkdir(exist_ok=False)
    shutil.copytree(STAGE/'manual',stage/'manual')
    for name in ('manual.tar.gz','manual-config.json','manual-entry.sh'):
        shutil.copy2(STAGE/name,stage/name)
        assert sha(stage/name)==sha(STAGE/name),name
    summary={'manual':dict(prior_summary['packages']['manual'])}
    summary['manual']['archive']=str(stage/'manual.tar.gz')
    assert sha(stage/'manual.tar.gz')==summary['manual']['archive_sha256']
    assert sha(stage/'manual/DAgger/F1_RELEASE_MANIFEST.json')==summary['manual']['manifest_sha256']
    manual_manifest=json.loads((stage/'manual/DAgger/F1_RELEASE_MANIFEST.json').read_text())
    for name,digest in manual_manifest['files'].items():
        assert sha(stage/'manual/DAgger'/name)==digest,name

    new=stage/'hil/DAgger'
    for rel,digest in manifest['files'].items():
        target=new/rel;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(base/rel,target)
        if rel in optimized_files:target.write_text(component['files'][rel])
        assert sha(target)==digest,rel
    changes={}
    for rel in sorted(save_files):
        assert sha(saved/rel)==saved_manifest['files'][rel],rel
        delta=''.join(difflib.unified_diff((base/rel).read_text().splitlines(True),
            (saved/rel).read_text().splitlines(True),fromfile='a/'+rel,tofile='b/'+rel))
        assert delta,rel
        subprocess.run(['patch','--batch','--fuzz=0','-p1'],input=delta,cwd=new,
            text=True,capture_output=True,check=True)
        assert sha(new/rel)==saved_manifest['files'][rel],rel
        changes[rel]=dict(before=manifest['files'][rel],after=sha(new/rel))
        manifest['files'][rel]=sha(new/rel)
    manifest['parent_manifest_sha256']=component['manifest_sha256']
    manifest['operator_finish']=saved_manifest['operator_finish']
    manifest['deployment_verified']=False
    dump(new/'F1_RELEASE_MANIFEST.json',manifest)
    archive=stage/'hil.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:
        for name in [*manifest['files'],'F1_RELEASE_MANIFEST.json']:
            tar.add(new/name,arcname='DAgger/'+name,recursive=False)
    summary['hil']=dict(destination='/home/user/Bruno/DAgger/releases/20261023-f1-hil-save-optimized-r1/DAgger',
        archive=str(archive),archive_sha256=sha(archive),manifest_sha256=sha(new/'F1_RELEASE_MANIFEST.json'),
        changed=changes,parent_release=component['release'],parent_manifest_sha256=component['manifest_sha256'])
    catalog['release']='unified-rlinf-save-20261023-r1'
    catalog['robot_release']='/home/user/Bruno/DAgger-runtime/'+catalog['release']
    binding=dict(client=summary['hil']['destination'],manifest_sha256=summary['hil']['manifest_sha256'])
    catalog['modes']['hil'].update(binding)
    default_hil['client_component']=binding.copy()
    dump(stage/'catalog.json',catalog)
    subprocess.run(['python3',str(assembly/'inference_runtime.py'),'--catalog',str(stage/'catalog.json'),
        '--build',str(stage/'assembly')],check=True)
    built=json.loads((stage/'assembly/assembly-manifest.json').read_text())
    assert built['files'].keys()==original_assembly['files'].keys()
    changed_assembly=[name for name,digest in original_assembly['files'].items() if built['files'][name]!=digest]
    assert changed_assembly==['catalog.json','configs/v1_dagger_rightonly_1000-hil.json'],changed_assembly
    for name,digest in built['files'].items():
        assert sha(stage/'assembly'/name)==digest,name
    dump(stage/'summary.json',dict(packages=summary,Y_unchanged=True,control_math_unchanged=True,
        windows_update_required=False,optimized_rebase=True,parent_assembly=remote['assembly'],
        parent_assembly_manifest_sha256=sha(assembly/'assembly-manifest.json'),
        preserved_optimized_files={name:manifest['files'][name] for name in sorted(optimized_files)},
        changed_assembly_files=changed_assembly))
    print(json.dumps(summary,ensure_ascii=False,indent=2))


def main():
    before=json.loads((OUT/'remote-before.json').read_text())
    STAGE.mkdir(exist_ok=False)
    source_before={
        'episode_controls.py':OUT/'before/DAgger/F1/src/f1dagger/episode_controls.py',
        'rtc_hil_feedback.py':OUT/'source-before/DAgger/F1/src/f1dagger/rtc_hil_feedback.py',
        'cli.py':OUT/'source-before/DAgger/F1/src/f1dagger/cli.py',
    }
    summary={}
    for mode in ('manual','hil'):
        old=PREVIOUS/mode/'DAgger';new=STAGE/mode/'DAgger'
        manifest=json.loads((old/'F1_RELEASE_MANIFEST.json').read_text())
        remote=before['files'][before[mode+'_release']+'/F1_RELEASE_MANIFEST.json']
        assert sha(old/'F1_RELEASE_MANIFEST.json')==remote['sha256']
        for rel,digest in manifest['files'].items():
            assert sha(old/rel)==digest,rel
            target=new/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(old/rel,target)
        changes={}
        for name,original in source_before.items():
            current=ROOT/'DAgger/F1/src/f1dagger'/name;rel='src/f1dagger/'+name
            delta=''.join(difflib.unified_diff(original.read_text().splitlines(True),current.read_text().splitlines(True),fromfile='a/'+rel,tofile='b/'+rel))
            assert delta,name
            subprocess.run(['patch','--batch','--fuzz=0','-p1'],input=delta,cwd=new,text=True,capture_output=True,check=True)
            changes[rel]=dict(before=manifest['files'][rel],after=sha(new/rel))
            manifest['files'][rel]=sha(new/rel)
        manifest['operator_finish']='dpad_up_no_home_or_terminal_hold_requirement_v1'
        manifest['deployment_verified']=False
        dump(new/'F1_RELEASE_MANIFEST.json',manifest)
        archive=STAGE/(mode+'.tar.gz')
        with tarfile.open(archive,'w:gz') as tar:
            for name in [*manifest['files'],'F1_RELEASE_MANIFEST.json']:tar.add(new/name,arcname='DAgger/'+name,recursive=False)
        summary[mode]=dict(destination='/home/user/Bruno/DAgger/releases/20261023-f1-'+mode+'-save-r1/DAgger',archive=str(archive),archive_sha256=sha(archive),manifest_sha256=sha(new/'F1_RELEASE_MANIFEST.json'),changed=changes)
    config=json.loads(before['files'][before['manual_config']]['text'])
    config['accepted_release_manifest_sha256']=summary['manual']['manifest_sha256'];dump(STAGE/'manual-config.json',config)
    entry=before['files']['/home/user/Bruno/DAgger/start_f1_hil_route.sh']['text']
    (STAGE/'manual-entry.sh').write_text(entry.replace(before['manual_release'],summary['manual']['destination']).replace(before['manual_config'],'/home/user/Bruno/DAgger-runtime/manual-save-20261023/config.json'))
    catalog=json.loads(before['files'][before['assembly']+'/catalog.json']['text'])
    # The editable catalog currently contains the separately paused RLinf
    # candidate. Keep that work intact and derive this patch from the installed
    # assembly, without activating its pending model/runtime changes.
    source_catalog=TASK/'configs/inference_runtime_f1_09.json'
    dump(OUT/'source-catalog-at-package.json',json.loads(source_catalog.read_text()))
    assert catalog==json.loads((PREVIOUS/'assembly/catalog.json').read_text())
    catalog['release']='unified-ab-full-save-20261023-r1';catalog['robot_release']='/home/user/Bruno/DAgger-runtime/'+catalog['release']
    catalog['modes']['hil'].update(client=summary['hil']['destination'],manifest_sha256=summary['hil']['manifest_sha256'])
    dump(STAGE/'catalog.json',catalog)
    # Reuse the installed assembly's generator, preserving unrelated pending work.
    subprocess.run(['python3',str(PREVIOUS/'assembly/inference_runtime.py'),'--catalog',str(STAGE/'catalog.json'),'--build',str(STAGE/'assembly')],check=True)
    old=json.loads((PREVIOUS/'assembly/assembly-manifest.json').read_text());new=json.loads((STAGE/'assembly/assembly-manifest.json').read_text())
    assert [n for n,d in old['files'].items() if new['files'][n]!=d]==['catalog.json','configs/v1_dagger_rightonly_1000-hil.json']
    dump(STAGE/'summary.json',dict(packages=summary,Y_unchanged=True,control_math_unchanged=True,windows_update_required=False))
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--optimized',action='store_true',help='rebase the frozen save patch onto the installed optimized HIL')
    args=parser.parse_args()
    if args.optimized:optimized()
    else:main()

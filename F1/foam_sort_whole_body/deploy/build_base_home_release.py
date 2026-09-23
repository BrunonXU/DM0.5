"""Build additive deployment; never overwrite the existing replay entry points."""
from pathlib import Path
import hashlib
import json
import shutil
import tarfile

TASK = Path(__file__).resolve().parents[1]
VERSION = '20260917-base-home-v1'
OLD = 'releases/20260916-recorder-ready-v1/workspace/F1/foam_sort_whole_body'
ROOT = '/home/user/Bruno/F1/foam_sort_whole_body'
OUT = TASK/'results/base_home_delivery_20260917'
SNAPSHOT = TASK/'results/deployments/20260916-recorder-ready-v1/payload'/OLD


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build():
    payload = OUT/'payload'
    if payload.exists():
        raise SystemExit('Payload exists; inspect rather than silently overwrite a release')
    release = payload/'releases'/VERSION
    release.mkdir(parents=True)
    shutil.copytree(SNAPSHOT/'robot', release/'robot', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copy2(TASK/'robot/f1_replay/base_home.py', release/'robot/f1_replay/base_home.py')
    shutil.copytree(OUT/'bundles', release/'bundles')
    (release/'configs').mkdir()
    for path in (release/'bundles').iterdir():
        sample = path.name[-3:]
        conf = json.loads((SNAPSHOT/'configs/pose_setup.json').read_text())
        conf['bundle'] = ROOT+'/releases/'+VERSION+'/bundles/'+path.name
        (release/'configs'/f'pose_{sample}.json').write_text(json.dumps(conf, indent=2)+'\n')
    for name in ('base_home.sh','pose_review.sh','replay_review.sh'):
        shutil.copy2(TASK/'scripts'/name, payload/name)
        (payload/name).chmod(0o755)
    baseline = {}
    for folder in ('robot', 'configs', 'scripts'):
        for path in (SNAPSHOT/folder).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts:
                baseline[OLD+'/'+str(path.relative_to(SNAPSHOT))] = sha(path)
    manifest = dict(version=VERSION, root=ROOT,
                    scope='onsite_diagnostic_not_physical_acceptance',
                    baseline_expected=baseline,
                    files={str(p.relative_to(payload)):dict(sha256=sha(p), mode=p.stat().st_mode & 0o777)
                           for p in sorted(payload.rglob('*')) if p.is_file()})
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    with tarfile.open(OUT/'release.tar.gz','w:gz') as tar:
        for name in manifest['files']:
            tar.add(payload/name,arcname=name,recursive=False)
        tar.add(OUT/'manifest.json',arcname='manifest.json')
    print(json.dumps({'archive':str(OUT/'release.tar.gz'),'sha256':sha(OUT/'release.tar.gz'),
                      'files':len(manifest['files'])},indent=2))


if __name__=='__main__':build()

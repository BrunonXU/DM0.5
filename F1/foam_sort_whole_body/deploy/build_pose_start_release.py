"""Add a versioned start-pose intersection policy; keep original data and tolerances."""
from pathlib import Path
import json,shutil,hashlib,tarfile
TASK=Path(__file__).resolve().parents[1]
OUT=TASK/'results/pose_start_intersection_20260917'
VERSION='20260917-pose-start-intersection-v1'
ROOT='/home/user/Bruno/F1/foam_sort_whole_body'
OLD=TASK/'results/deployments/20260916-recorder-ready-v1/payload/releases/20260916-recorder-ready-v1/workspace/F1/foam_sort_whole_body'
HOME=TASK/'results/base_home_delivery_20260917/payload'

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
 payload=OUT/'payload';release=payload/'releases'/VERSION
 release.mkdir(parents=True,exist_ok=False)
 shutil.copytree(OLD/'robot', release/'robot',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
 for name in ['pose_cli.py','pose_model.py']:shutil.copy2(TASK/'robot/f1_replay'/name,release/'robot/f1_replay'/name)
 shutil.copytree(HOME/'releases/20260917-base-home-v1/configs',release/'configs')
 for sample in ['020','096']:
  p=release/'configs'/f'pose_{sample}.json';d=json.loads(p.read_text());d['start_pose_policy']='feasible_intersection_v1';p.write_text(json.dumps(d,indent=2)+'\n')
 entry=(HOME/'pose_review.sh').read_text().replace('releases/20260917-base-home-v1','releases/'+VERSION).replace('PYTHONPATH="$client/robot','PYTHONPATH="$release/robot')
 (payload/'pose_review.sh').write_text(entry);(payload/'pose_review.sh').chmod(0o755)
 baseline=json.loads((TASK/'results/base_home_delivery_20260917/manifest.json').read_text())['baseline_expected']
 baseline['pose_review.sh']=sha(HOME/'pose_review.sh')
 # Preserve bindings to the already deployed review bundles and configs.
 for path in (HOME/'releases/20260917-base-home-v1').rglob('*'):
  if path.is_file() and any(part in ('bundles','configs') for part in path.parts):baseline[str(path.relative_to(HOME))]=sha(path)
 manifest={'version':VERSION,'root':ROOT,'baseline_expected':baseline,'files':{str(p.relative_to(payload)):{'sha256':sha(p),'mode':p.stat().st_mode&0o777} for p in sorted(payload.rglob('*')) if p.is_file()}}
 (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
 with tarfile.open(OUT/'release.tar.gz','w:gz') as t:
  for name in manifest['files']:t.add(payload/name,arcname=name,recursive=False)
  t.add(OUT/'manifest.json',arcname='manifest.json')
 print('files',len(manifest['files']))

if __name__=='__main__':main()

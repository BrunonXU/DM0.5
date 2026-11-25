"""Prepare and run a frozen full-owner replay with explicit inputs and no hardware.

Only the offline replay/audit programs are launched. The production site is
copied, the original clocks and fixture inputs remain recorded, and all later
plant/attachment feedback is explicitly simulated by replay_full_owner.py.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def read(path):return json.loads(Path(path).read_text())
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write(path,value):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    Path(path).write_text(json.dumps(value,indent=2)+'\n')


def environment(root,repository):
    control=(root/'control/src',) if (root/'control/src').is_dir() else (
        repository.parent/'DAgger/F1/src',repository.parent/'DAgger/shared/src')
    return dict(os.environ,PYTHONPATH=os.pathsep.join(map(str,(
        root/'src',*control,repository/'.overlay',repository/'third_party/RPent'))),
        OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',PYTHONDONTWRITEBYTECODE='1')


def fixture_options(path):
    command=read(path)['command']; result={};index=2
    while index<len(command):
        key=command[index]
        if not key.startswith('--'):raise ValueError('invalid_recorded_command')
        value=True if index+1==len(command) or command[index+1].startswith('--') else command[index+1]
        result.setdefault(key,[]).append(value)
        index+=1 if value is True else 2
    return result


def prepare(args):
    root,site,output,repository=args.root,args.site,args.output,args.repository
    runtime=output/'offline/HARNESS';local_site=output/'offline/site'
    if runtime.exists() or local_site.exists():raise FileExistsError('offline runtime/site already exist')
    manifest=read(root/'manifest.json')
    runtime.mkdir(parents=True)
    for folder in ('src','scripts','control','native','configs','upstream'):
        if (root/folder).exists():
            shutil.copytree(root/folder,runtime/folder,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in ('.venv','.venv-numerics','.overlay','third_party'):
        (runtime/name).symlink_to(repository/name,target_is_directory=True)
    (runtime.parent/'F1-4242').symlink_to(repository.parent/'F1-4242',target_is_directory=True)
    source_hashes={str(p.relative_to(runtime)):sha(p) for folder in ('src','scripts','control')
                   for p in (runtime/folder).rglob('*.py')}
    for name,value in source_hashes.items():
        if sha(root/name)!=value:raise ValueError('frozen_copy_changed:'+name)
    shutil.copytree(site,local_site)
    source_manifest=output/'offline/source-manifest.json';write(source_manifest,manifest)
    assets=dict(urdf_path=str(repository.parent/'F1-4242/web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf'),
                mapping_path=str(repository.parent/'F1-4242/configs/model_mapping.json'))
    def relocate(value):
        if isinstance(value,dict):return {k:assets.get(k,relocate(v)) for k,v in value.items()}
        if isinstance(value,list):return [relocate(v) for v in value]
        if isinstance(value,str) and value.startswith('/') and (site/Path(value).name).is_file():
            return str(local_site/Path(value).name)
        return value
    for path in local_site.glob('*.json'):
        if path.name not in ('coarse-calibration.json','controller-reuse.json'):
            write(path,relocate(read(path)))
    masks=read(local_site/'masks.local.json')
    for view in masks['views'].values():view['sam_url']=args.mask_url
    write(local_site/'masks.local.json',masks)
    development=read(local_site/'development.local.json')
    development['code']['bundle_manifest_path']=str(source_manifest)
    development['model'].update(assets)
    development['reviewed_files']={name:sha(local_site/name) for name in development['reviewed_files']}
    development['offline_derivation']=dict(source=str(site),hardware_outputs=0,
        physical_parameters_changed=False,source_manifest=str(source_manifest))
    write(local_site/'development.local.json',development)
    original=fixture_options(args.fixture_command)
    perception=read(original['--rpc-perception-config'][0])
    perception.update(mask_config=str(local_site/'masks.local.json'),sam_url=args.mask_url)
    write(output/'perception.json',perception)
    receipt=dict(schema='f1-explicit-offline-runtime-v1',root=str(runtime),site=str(local_site),
        frozen_root=str(root),production_site=str(site),bundle_id=manifest['bundle_id'],
        source_hashes=source_hashes,fixture_command=str(args.fixture_command),
        fixture_command_sha256=sha(args.fixture_command),physical_motion=False,
        scripts_copied_without_text_rewrites=True,driver_sha256=sha(__file__))
    write(output/'offline-bindings.json',receipt)
    print(json.dumps(receipt|{'source_hashes':len(source_hashes)},indent=2))


def run(args):
    root,output=args.root,args.output
    output.mkdir(parents=True,exist_ok=False)
    options=fixture_options(args.fixture_command)
    modes={'normal':'ideal','grasp-observation':'recover_after_motion','grasp-miss':'miss_once_then_observe','aperture-occluded':'ideal'}
    command=[str(args.python),str(root/'scripts/replay_full_owner.py'),
        '--site',str(args.site),'--output',str(output/'replay'),
        '--rpc-perception-config',str(args.perception),'--grasp-observation-evidence',modes[args.case],
        '--rpc-planner','recorded','--program','registered','--follower','approved-output',
        '--max-wall-seconds',str(args.max_wall_seconds),'--wall-clock','--defer-fixture-cyclic-gc']
    if args.case=='aperture-occluded':
        command.append('--occlude-aperture-after-transfer')
    if args.simulated_gripper_can:
        command.append('--simulated-gripper-can')
    for name in ('--scene','--scene-index','--approach-request','--rpc-capture','--rpc-hint-receipt'):
        if name not in options and name!='--scene-index':raise ValueError('missing_recorded_fixture:'+name)
        for value in options.get(name,['0']):command.extend((name,str(value)))
    env=environment(root,args.repository)
    write(output/'command.json',dict(command=command,PYTHONPATH=env['PYTHONPATH'],
        source_hashes={str(root/'scripts'/name):sha(root/'scripts'/name) for name in (
            'replay_full_owner.py','replay_planning_process.py','replay_rpc_session.py')},
        physical_motion=False,initial_rgbd_and_sam=True,later_plant_and_feedback_simulated=True,
        case=args.case,driver_sha256=sha(__file__)))
    with (output/'run.log').open('w') as log:
        result=subprocess.run(command,cwd=args.repository.parent,env=env,stdout=log,stderr=subprocess.STDOUT)
    write(output/'status.json',dict(returncode=result.returncode,case=args.case,physical_motion=False))
    print(result.returncode,flush=True)
    return result.returncode


def audit(args):
    output=args.output;output.mkdir(parents=True,exist_ok=False)
    report=read(args.report);stages={row['skill'] for row in report['stages']}
    names=['replay_output','grasp_output']
    if 'recover_grasp' in stages:names.append('grasp_observation_output')
    commands={name:[str(args.python),str(args.root/'scripts'/('audit_'+name+'.py')),
        '--report',str(args.report),'--output',str(output/(name+'.json'))] for name in (*names,'cycle_output')}
    for name in names:commands['cycle_output']+=['--prior-audit',str(output/(name+'.json'))]
    env=environment(args.root,args.repository)
    write(output/'commands.json',dict(commands=commands,PYTHONPATH=env['PYTHONPATH'],physical_motion=False))
    def invoke(name):
        with (output/(name+'.log')).open('w') as log:
            result=subprocess.run(commands[name],cwd=args.repository.parent,env=env,stdout=log,stderr=subprocess.STDOUT)
        return name,result.returncode
    with ThreadPoolExecutor(max_workers=3) as executor:results=dict(executor.map(invoke,names))
    if all(code==0 for code in results.values()):
        name,code=invoke('cycle_output');results[name]=code
    not_applicable=[] if 'recover_grasp' in stages else ['grasp_observation_output:no_observation_motion_in_this_case']
    passed=len(results)==len(commands) and all(code==0 for code in results.values())
    write(output/'status.json',dict(results=results,not_applicable=not_applicable,passed=passed,physical_motion=False))
    print(json.dumps(dict(results=results,not_applicable=not_applicable,passed=passed),indent=2))
    return 0 if passed else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('prepare','run','audit'))
    parser.add_argument('--root',type=Path,required=True,help='frozen root for prepare; copied runtime root otherwise')
    parser.add_argument('--site',type=Path,help='source site for prepare; copied site for run')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--repository',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--fixture-command',type=Path)
    parser.add_argument('--mask-url')
    parser.add_argument('--perception',type=Path)
    parser.add_argument('--python',type=Path)
    parser.add_argument('--case',choices=('normal','grasp-observation','grasp-miss','aperture-occluded'),default='normal')
    parser.add_argument('--max-wall-seconds',type=float,default=240.)
    parser.add_argument('--simulated-gripper-can',action='store_true',
                        help='Use independent simulated encoder readings for grasp verification')
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    for name in ('root','site','output','repository','fixture_command','perception','report'):
        if getattr(args,name) is not None:setattr(args,name,getattr(args,name).resolve())
    if args.python is None:args.python=args.repository/'.venv-numerics/bin/python'
    required={'prepare':('site','fixture_command','mask_url'),'run':('site','fixture_command','perception'),'audit':('report',)}
    for name in required[args.operation]:
        if getattr(args,name) is None:parser.error('--'+name.replace('_','-')+' is required for '+args.operation)
    return {'prepare':prepare,'run':run,'audit':audit}[args.operation](args) or 0


if __name__=='__main__':raise SystemExit(main())

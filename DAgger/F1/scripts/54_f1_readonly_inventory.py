#!/usr/bin/env python3
"""Read F1 machine/dependency evidence over explicit SSH; never upload or start ROS."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--ssh-config', required=True, type=Path)
    parser.add_argument('--infra-root', required=True)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--python', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.host.startswith('-') or not args.ssh_config.is_file():
        parser.error('explicit valid SSH host/config required')
    if args.output.exists():
        parser.error('refusing to overwrite existing evidence')
    binding_path = ROOT/'configs/f1_infra_binding.json'
    request = dict(root=args.infra_root, profile=args.profile, python=args.python,
                   files=json.loads(binding_path.read_text())['files'])
    remote = 'request = ' + repr(request) + '\n' + r'''
import hashlib, json, platform, subprocess, sys
from pathlib import Path
root = Path(request['root']).resolve(strict=True)
profile_path = Path(request['profile']).resolve(strict=True)
profile = json.loads(profile_path.read_text())
checks = []
for relative, expected in request['files'].items():
    path = (root/relative).resolve()
    actual = hashlib.sha256(path.read_bytes()).hexdigest() if root in path.parents and path.is_file() else None
    checks.append(dict(path=relative, expected=expected, actual=actual, matches=actual == expected))
probe = """
import importlib.metadata,json,sys
names=('numpy','Pillow','PyYAML','pin','pin-pink','qpsolvers','proxsuite','ruckig','mujoco','cmeel-urdfdom','cmeel-tinyxml2')
versions={}
for name in names:
 try:versions[name]=importlib.metadata.version(name)
 except importlib.metadata.PackageNotFoundError:versions[name]=None
print(json.dumps(dict(executable=sys.executable,python=sys.version.split()[0],versions=versions)))
"""
dependencies = subprocess.run([request['python'],'-c',probe],capture_output=True,text=True,timeout=10,check=True)
print(json.dumps(dict(machine_id=Path('/etc/machine-id').read_text().strip(),architecture=platform.machine(),
    system_python=sys.version.split()[0],infra_root=str(root),profile_path=str(profile_path),
    profile_sha256=hashlib.sha256(profile_path.read_bytes()).hexdigest(),
    profile={k:profile.get(k) for k in ('robot_id','version','expected_machine_id','motion_enabled','joints_enabled','limits_verified','control_contract_verified','gripper_verified')},
    assets=checks,assets_match=all(item['matches'] for item in checks),dependencies=json.loads(dependencies.stdout),
    ros_initialized=False,command_endpoints_created=False)))
'''
    result = subprocess.run(['ssh','-F',str(args.ssh_config.resolve()),'-o','BatchMode=yes',args.host,'python3','-'],
                            input=remote,text=True,capture_output=True,timeout=30)
    if result.returncode:
        raise RuntimeError('read-only inventory failed: '+result.stderr.strip())
    evidence = json.loads(result.stdout)
    evidence.update(checked_at_utc=datetime.now(timezone.utc).isoformat(),ssh_host=args.host,
                    binding_sha256=hashlib.sha256(binding_path.read_bytes()).hexdigest(),
                    scope='read_only_machine_files_and_package_metadata; not runtime_contract or motion acceptance')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x') as stream:
        json.dump(evidence,stream,indent=2)
    print(json.dumps(dict(evidence=str(args.output),assets_match=evidence['assets_match'],
        mismatches=[row['path'] for row in evidence['assets'] if not row['matches']],dependencies=evidence['dependencies']),indent=2))
    return 0 if evidence['assets_match'] else 1

if __name__ == '__main__':
    raise SystemExit(main())

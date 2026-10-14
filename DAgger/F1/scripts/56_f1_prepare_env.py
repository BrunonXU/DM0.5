#!/usr/bin/env python3
"""Create a new F1 ARM64 Python environment; no ROS/control imports or system edits."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--lock',required=True,type=Path)
    parser.add_argument('--lock-sha256',required=True)
    args=parser.parse_args()
    root=args.root.resolve()
    if Path('/home/user/Bruno') not in root.parents:
        parser.error('F1 runtime must be inside /home/user/Bruno')
    if platform.machine()!='aarch64' or sys.version_info[:2]!=(3,12):
        parser.error('run on F1 ARM64 with its Python 3.12')
    lock=args.lock.resolve(strict=True)
    if hashlib.sha256(lock.read_bytes()).hexdigest()!=args.lock_sha256:
        parser.error('dependency lock hash mismatch')
    venv=root/'f1-09-venv'
    result=root/'environment-preparation.json'
    if venv.exists() or result.exists():
        parser.error('existing environment/evidence: inspect and resume explicitly; never overwrite')
    root.mkdir(parents=True,exist_ok=True)
    for folder in ('build-tmp','pip-cache','cache'):(root/folder).mkdir(exist_ok=True)
    environment=dict(os.environ,TMPDIR=str(root/'build-tmp'),PIP_CACHE_DIR=str(root/'pip-cache'),
        XDG_CACHE_HOME=str(root/'cache'),PIP_CONFIG_FILE='/dev/null',PIP_DISABLE_PIP_VERSION_CHECK='1',
        PYTHONDONTWRITEBYTECODE='1',CMAKE_BUILD_PARALLEL_LEVEL='1',MAKEFLAGS='-j1')
    report=dict(started_utc=datetime.now(timezone.utc).isoformat(),venv=str(venv),
        lock_sha256=args.lock_sha256,architecture=platform.machine(),python=sys.version,
        status='running',robot_control_started=False)
    try:
        subprocess.run([sys.executable,'-m','venv',str(venv)],check=True,env=environment)
        subprocess.run([str(venv/'bin/python'),'-m','pip','install','--index-url','https://pypi.org/simple',
                        '--require-hashes','-r',str(lock)],check=True,env=environment)
        subprocess.run([str(venv/'bin/python'),'-m','pip','check'],check=True,env=environment)
        probe="import pinocchio,pink,ruckig,mujoco,numpy;print('F1_NUMERICAL_IMPORTS_OK',pinocchio.__version__,pink.__version__,mujoco.__version__,numpy.__version__)"
        subprocess.run([str(venv/'bin/python'),'-c',probe],check=True,env=environment)
        report['status']='installed_imports_verified'
    except BaseException as exc:
        report['status']='failed';report['error']=f'{type(exc).__name__}: {exc}'
        raise
    finally:
        report['finished_utc']=datetime.now(timezone.utc).isoformat()
        with result.open('x') as stream:json.dump(report,stream,indent=2)
    return 0

if __name__=='__main__':
    raise SystemExit(main())

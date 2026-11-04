"""Stage a new immutable runtime from the active frozen H20 source, without activation."""
import hashlib
import json
from pathlib import Path
import shutil


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    staging = Path(__file__).parent
    base = Path('/home/user/projects/task-releases/f1-toys-20261020-rtc-h20-v3')
    target = base.with_name('f1-toys-20261020-rtc-h20-v4-fastmath')
    assert sha(base/'src/f1_toys/native/kinematics.py') == 'c9e4acd0bbcdf05c263f9ab6a1dacd24caeb91a6adf440faabedb9db1d5cdd36'
    target.mkdir(exist_ok=False)
    shutil.copytree(base/'src', target/'src', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copy2(staging/'src/f1_toys/native/fast_kinematics.py',
                 target/'src/f1_toys/native/fast_kinematics.py')
    shutil.copy2(staging/'h20_rtc_fast_server.py', target/'h20_rtc_fast_server.py')
    starts = {}
    for step in ('5000', '50000'):
        oldrun = Path(f'/u01/user/runs/f1-toys-rtc-h20-{step}-graph-20261020')
        newrun = oldrun.with_name(f'f1-toys-rtc-h20-{step}-fast-20261020')
        newrun.mkdir(exist_ok=False)
        script = (oldrun/'start-v3.sh').read_text().replace(str(base), str(target))
        script = script.replace('h20_graph_server.py', 'h20_rtc_fast_server.py')
        # Reuse only the compiled-model cache; new request logs and console.
        script = script.replace(f'mkdir -p {oldrun}/logs', f'mkdir -p {newrun}/logs')
        script = script.replace(f'--log {oldrun}/logs/', f'--log {newrun}/logs/')
        (newrun/'start.sh').write_text(script)
        starts[step] = str(newrun/'start.sh')
    receipt = dict(base=str(base), release=str(target), starts=starts, activated=False,
                   files_sha256={str(p.relative_to(target)): sha(p)
                                 for p in target.rglob('*') if p.is_file()})
    (target/'SOURCE_MANIFEST.json').write_text(json.dumps(receipt, indent=2))
    print(json.dumps({'release': str(target), 'starts': starts,
                      'files': len(receipt['files_sha256']), 'activated': False}), flush=True)


if __name__ == '__main__':
    main()

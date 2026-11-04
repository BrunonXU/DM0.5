#!/usr/bin/env python3
"""Mirror Projects/infra and its two collaboration entry files from 6000D to H20.

Default: read-only comparison. --apply: copy and verify every SHA256.
Never deletes files; aborts on untracked/differing H20 content or symlinks.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import tempfile


PROJECTS = Path(__file__).resolve().parents[3]
TARGET = "/home/user/projects"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
       "-o", "HostKeyAlgorithms=ssh-ed25519", "h20-train"]


def run(argv, **kwargs):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=90, **kwargs)
    if result.returncode:
        raise RuntimeError(f"Command failed: {shlex.join(argv)}\n{result.stderr}")
    return result.stdout


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local_manifest():
    paths = [PROJECTS / "AGENTS.md", PROJECTS / "PROJECT_RULES_AND_PREFERENCES.md"]
    infra = PROJECTS / "infra"
    for path in [infra, *paths]:
        if path.is_symlink() or path.resolve() != path:
            raise RuntimeError(f"Refusing source symlink path: {path}")
    if not infra.is_dir() or not all(path.is_file() for path in paths):
        raise RuntimeError("Source infra directory or collaboration entry files missing")
    for path in sorted(infra.rglob("*")):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise RuntimeError(f"Refusing source symlink: {path}")
        if path.is_file():
            paths.append(path)
    return {str(p.relative_to(PROJECTS)): digest(p) for p in paths}


def remote_snapshot(manifest):
    code = f"""import hashlib,json,os
from pathlib import Path
root=Path({TARGET!r})
assert os.getuid()==1004 and root.resolve()==root
names={list(manifest)!r}
out={{'files':{{}}, 'extra_infra':[],'previous':{{}}}}
state=root/'.infra-sync-state.json'
if state.is_symlink():raise RuntimeError('Refusing symlink state')
if state.exists():out['previous']=json.loads(state.read_text()).get('files',{{}})
for name in names:
 p=root/name
 if p.is_symlink() or p.resolve()!=p:raise RuntimeError('Refusing symlink path: '+name)
 if p.exists():
  if not p.is_file():raise RuntimeError('Not a file: '+name)
  out['files'][name]=hashlib.sha256(p.read_bytes()).hexdigest()
if (root/'infra').exists():
 for p in (root/'infra').rglob('*'):
  if p.is_symlink():raise RuntimeError('Refusing remote symlink: '+str(p))
  if p.is_file() and str(p.relative_to(root)) not in names:
   out['extra_infra'].append(str(p.relative_to(root)))
print(json.dumps(out))
"""
    return json.loads(run(SSH + ["python3 -B -"], input=code))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    if args.receipt:
        receipt = args.receipt.resolve()
        if receipt.exists():
            parser.error("Receipt already exists; choose a new filename")
        if receipt.is_relative_to(PROJECTS / "infra"):
            parser.error("Put this run's receipt outside infra to avoid self-referential hashes")
    config = dict(line.split(" ", 1) for line in run(["ssh", "-G", "h20-train"]).splitlines() if " " in line)
    for key, expected in [("hostname", "192.0.2.27"), ("port", "6622"), ("user", "user")]:
        if config.get(key) != expected:
            raise RuntimeError(f"Unexpected SSH alias {key}: {config.get(key)}")
    manifest = local_manifest()
    before = remote_snapshot(manifest)
    conflicts = [name for name, value in before['files'].items()
                 if value != manifest[name] and value != before['previous'].get(name)]
    if conflicts or before['extra_infra']:
        raise RuntimeError(f"Review H20 edits first. Conflicts={conflicts}; extra={before['extra_infra']}")
    changes = [name for name, value in manifest.items() if before['files'].get(name) != value]
    print(json.dumps({'mode': 'apply' if args.apply else 'check', 'source': str(PROJECTS),
                      'target': TARGET, 'file_count': len(manifest), 'changes': changes}, indent=2))
    if not args.apply:
        return
    # Target projects already belongs to the personal user. No global directories touched.
    with tempfile.TemporaryDirectory(prefix='h20-doc-sync-') as tmp:
        file_list = Path(tmp) / 'files.txt'
        file_list.write_text('\n'.join(manifest) + '\n')
        output = run(['rsync', '-rlt', '--checksum', '--itemize-changes',
                      '--files-from=' + str(file_list), '-e', shlex.join(SSH[:-1]),
                      str(PROJECTS) + '/', 'h20-train:' + TARGET + '/'])
    if local_manifest() != manifest:
        raise RuntimeError('Source changed during sync; rerun after edits finish')
    after = remote_snapshot(manifest)
    if after['files'] != manifest or after['extra_infra']:
        raise RuntimeError('Remote path set / SHA256 does not match source')
    report = {'verified_at_utc': datetime.now(timezone.utc).isoformat(),
              'source_machine': '6000D', 'source': str(PROJECTS),
              'target_host': '192.0.2.27', 'target_port': 6622, 'target': TARGET,
              'scope': ['infra/', 'AGENTS.md', 'PROJECT_RULES_AND_PREFERENCES.md'],
              'file_count': len(manifest), 'files': manifest, 'result': 'PASS'}
    state_code = f"""import json,os,tempfile
from pathlib import Path
root=Path({TARGET!r})
assert os.getuid()==1004 and root.resolve()==root
state=root/'.infra-sync-state.json'
if state.is_symlink():raise RuntimeError('Refusing symlink state')
fd,name=tempfile.mkstemp(prefix='.infra-sync-',dir=root)
with os.fdopen(fd,'w') as f:f.write({json.dumps(report, indent=2) + chr(10)!r})
os.replace(name,state)
print('STATE_SAVED')
"""
    run(SSH + ['python3 -B -'], input=state_code)
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        with args.receipt.open('x') as f:
            json.dump(report, f, indent=2)
            f.write('\n')
    print(output.rstrip())
    print(f"PASS: {len(manifest)} files match by path and SHA256; H20 state saved")


if __name__ == '__main__':
    main()

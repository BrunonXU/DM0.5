#!/usr/bin/env python3
"""Read-only bounded F1 source capture; never imports captured robot code."""
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
SSH = ["ssh", "-F", "/dev/null", "-i", "/home/xza_100850113/.ssh/id_ed25519_f1",
       "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
       "-o", "HostKeyAlias=f1-d070", "-o", "ConnectTimeout=5", "-p", "22022", "user@127.0.0.1"]
REMOTE = r'''
import base64, datetime, hashlib, json, pathlib, re, socket, subprocess
paths = PATHS_LITERAL
root = pathlib.Path('/home/user/meaxiao/pico')
def git_run(parent, args):
    p = subprocess.run(['git', '-C', str(parent), *args], capture_output=True, text=True, timeout=10)
    return p.stdout.strip() if p.returncode == 0 else None
rows = []
for value in paths:
    p = pathlib.Path(value)
    resolved = p.resolve(strict=True)
    if not resolved.is_relative_to(root): raise RuntimeError('resolved path outside approved source tree')
    if not p.is_file() or p.stat().st_size > 1000000: raise RuntimeError('file type or size outside approved scope')
    data = p.read_bytes()
    if len(data) > 1000000: raise RuntimeError('file grew beyond scope')
    if re.search(rb'-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----', data): raise RuntimeError('private key pattern; refusing transfer')
    if re.search(rb'(?im)^\s*(?:password|passwd|api_key|access_token|secret_key)\s*=\s*[\x22\x27][^\x22\x27]{4,}', data): raise RuntimeError('possible literal credential; refusing transfer')
    repo = git_run(p.parent, ['rev-parse', '--show-toplevel'])
    git = {'available': bool(repo)}
    if repo:
        rel = str(p.relative_to(pathlib.Path(repo)))
        git.update(worktree_root=repo, commit=git_run(p.parent, ['rev-parse','HEAD']),
            tracked_worktree_dirty=bool(git_run(p.parent, ['status','--porcelain','--untracked-files=no'])),
            file_status=git_run(p.parent, ['status','--porcelain','--untracked-files=normal','--',str(p)]),
            untracked_other_files='not_scanned')
    rows.append(dict(source_host=socket.gethostname(), source_path=str(p), resolved_path=str(resolved),
        source_is_symlink=p.is_symlink(), captured_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), git=git,
        content_b64=base64.b64encode(data).decode('ascii')))
print(json.dumps(rows))
'''

paths = sys.argv[1:]
if not paths or len(paths) > 6:
    raise SystemExit('Provide one to six explicit paths')
manifest_path = ROOT / 'manifest.json'
manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {
    'source_host': 'ms-thor', 'snapshot_kind': 'selected_action_mapping_dependency_evidence',
    'remote_mutations': False, 'scope_limit_files': 6, 'scope_limit_bytes_per_file': 1000000,
    'secret_screening': 'Heuristic private-key and literal-credential scan before transfer; not security certification.',
    'files': []}
if len(manifest['files']) + len(paths) > 6:
    raise SystemExit('Snapshot six-file limit exceeded')
for p in paths:
    if (ROOT / 'files' / p.lstrip('/')).exists():
        raise SystemExit('Refusing to replace existing snapshot file: ' + p)
script = REMOTE.replace('PATHS_LITERAL', repr(paths))
result = subprocess.run([*SSH, 'python3 -'], input=script, text=True, capture_output=True, timeout=90, check=True)
rows = json.loads(result.stdout)
for row in rows:
    data = base64.b64decode(row.pop('content_b64'), validate=True)
    assert len(data) == row['bytes'] and hashlib.sha256(data).hexdigest() == row['sha256']
    relative = Path('files') / row['source_path'].lstrip('/')
    dest = ROOT / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open('xb') as stream:
        stream.write(data)
    local_sha = hashlib.sha256(dest.read_bytes()).hexdigest()
    assert local_sha == row['sha256']
    row.update(local_path=str(relative), local_sha256=local_sha, local_sha256_verified=True)
    manifest['files'].append(row)
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'captured': len(rows), 'total_files': len(manifest['files']), 'total_bytes': sum(x['bytes'] for x in manifest['files']), 'hashes_verified': True}))

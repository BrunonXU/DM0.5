#!/usr/bin/env python3
"""Freeze this task and its minimal RLinf runtime; stage immutable H20 files."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

TASK = Path(__file__).resolve().parents[1]
PROJECTS = TASK.parents[1]
SSH = ["ssh", "-oBatchMode=yes", "h20-train"]
CACHE_DIRS = {"__pycache__", ".pytest_cache", ".git"}
SOURCE_SCOPE = "task-source-and-markdown-v1"


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8*1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()


def remote(code):
    return subprocess.check_output(SSH+["python3 -"], input=code, text=True)


def source_files():
    """Include runtime assets and docs; leave generated evidence at its origin."""
    selected = [TASK, PROJECTS/"RLinf/rlinf", PROJECTS/"RLinf/examples/sft", PROJECTS/"RLinf/LICENSE"]
    for source in selected:
        if source.is_symlink():
            continue
        if source.is_dir():
            for directory, directories, names in os.walk(source, followlinks=False):
                root = Path(directory)
                # Prune artifacts before walking: staging itself lives below it.
                directories[:] = sorted(name for name in directories
                                        if name not in CACHE_DIRS | {"artifacts"}
                                        and not (root/name).is_symlink())
                for name in sorted(names):
                    path = root/name
                    if path.is_symlink() or not path.is_file() or path.suffix == ".pyc":
                        continue
                    if path.is_relative_to(TASK):
                        relative = path.relative_to(TASK)
                        if (relative.parts[0] == "Eval" or "visualizations" in relative.parts) and path.suffix.lower() != ".md":
                            continue
                    yield path
        elif source.is_file():
            yield source
    # Keep the artifact navigation without copying datasets, models or releases.
    readme = TASK / "artifacts/README.md"
    if readme.is_file() and not readme.is_symlink():
        yield readme


def build_packet(packet):
    """Create a new local code packet, without contacting H20 or starting work."""
    packet.mkdir(parents=True, exist_ok=False)
    files = {}
    for path in source_files():
        relative = path.relative_to(PROJECTS)
        destination = packet/relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        files[str(relative)] = digest(destination)
    return files


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--release", required=True)
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--raw", type=Path, help="Optional when original raw records are already archived on H20")
    p.add_argument("--remote-data-root", default="/u01/user/datasets/f1_toys_v1_smoke_20261015")
    p.add_argument("--activate-task", action="store_true", help="Point the H20 task entry at this verified release")
    args = p.parse_args()
    if not all(c.isalnum() or c in "-_." for c in args.release):
        raise ValueError("Invalid release name")
    packet = TASK / "artifacts/h20-staging" / args.release
    files = build_packet(packet)
    manifest = dict(release=args.release, files=files,
                    rlinf_commit=subprocess.check_output(["git","-C",str(PROJECTS/"RLinf"),"rev-parse","HEAD"],text=True).strip(),
                    dataset_manifest_sha256=digest(args.dataset/"manifest.json"), source="6000D authoritative task snapshot",
                    source_scope=SOURCE_SCOPE)
    (packet/"SOURCE_MANIFEST.json").write_text(json.dumps(manifest, indent=2)+"\n")
    remote_release = "/home/user/projects/task-releases/"+args.release
    remote_data = args.remote_data_root
    if not remote_data.startswith("/u01/user/datasets/") or ".." in Path(remote_data).parts:
        raise ValueError("Data must stay under the personal H20 dataset root")
    # Existing immutable data must match before rsync can write anything.
    protected = {}
    data_transfers = [(args.dataset, remote_data+"/dataset")]
    if args.raw:
        data_transfers.append((args.raw, remote_data+"/raw/"+args.raw.name))
    for source, target in data_transfers:
        protected[target] = {str(f.relative_to(source)): digest(f) for f in source.rglob("*") if f.is_file()}
    print(remote(f"""from pathlib import Path
import os,shutil,hashlib
assert os.getuid()==1004 and os.path.ismount('/u01')
assert shutil.disk_usage('/u01').free>100*1024**3
for target,files in {protected!r}.items():
 for name,expected in files.items():
  existing=Path(target)/name
  if existing.exists():
   h=hashlib.sha256()
   with existing.open('rb') as stream:
    for chunk in iter(lambda:stream.read(8*1024*1024),b''):h.update(chunk)
   assert h.hexdigest()==expected, f'Refusing to overwrite changed immutable data: {{existing}}'
p=Path({remote_release!r});p.mkdir(parents=True,exist_ok=False)
Path({remote_data!r}).mkdir(parents=True,exist_ok=True)
print(p)
"""), flush=True)
    for source, target in [(packet, remote_release), *data_transfers]:
        # Preview records exact scope; transfer keeps existing raw/data bytes protected.
        base=["rsync","-rlt","--checksum","--partial","--partial-dir=.rsync-partial","--mkpath","-e","ssh -oBatchMode=yes"]
        subprocess.run(base+["--dry-run","--stats",str(source)+"/","h20-train:"+target+"/"],check=True)
        subprocess.run(base+["--info=stats2",str(source)+"/","h20-train:"+target+"/"],check=True)
    verify = remote(f"""from pathlib import Path
import hashlib,json
root=Path({remote_release!r});m=json.loads((root/'SOURCE_MANIFEST.json').read_text())
for name,expected in m['files'].items():
 assert hashlib.sha256((root/name).read_bytes()).hexdigest()==expected,name
data=Path({remote_data!r})/'dataset'
assert hashlib.sha256((data/'manifest.json').read_bytes()).hexdigest()==m['dataset_manifest_sha256']
dm=json.loads((data/'manifest.json').read_text())
for name,expected in dm['files_sha256'].items():
 assert hashlib.sha256((data/name).read_bytes()).hexdigest()==expected,name
print(json.dumps(dict(result='PASS',release=str(root),dataset=str(data),code_files=len(m['files']),data_files=len(dm['files_sha256']))))
""")
    (packet/"H20_RECEIPT.json").write_text(verify)
    print(verify, flush=True)
    if args.activate_task:
        receipt = remote(f"""from pathlib import Path
import json,os,uuid,hashlib
entry=Path('/home/user/projects/tasks/F1_ComplexyToys')
target=Path({remote_release!r})/'tasks/F1_ComplexyToys'
if entry.exists() or entry.is_symlink():
 assert entry.is_symlink(), 'Refusing to replace an existing task directory'
 assert entry.resolve().is_relative_to(Path('/home/user/projects/task-releases')), 'Unmanaged task link'
entry.parent.mkdir(parents=True,exist_ok=True)
temporary=entry.with_name(entry.name+'.'+uuid.uuid4().hex)
temporary.symlink_to(target)
os.replace(temporary,entry)
assert entry.resolve()==target
print(json.dumps(dict(task=str(entry),target=str(target),readme_sha256=hashlib.sha256((entry/'README.md').read_bytes()).hexdigest())))
""")
        (packet/"TASK_POINTER_RECEIPT.json").write_text(receipt)
        print(receipt, flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Transfer only verified base objects to the user's H20 model directory."""
import datetime
import concurrent.futures
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import time

HERE = Path(__file__).resolve().parent
MANIFEST = json.loads((HERE / "base-manifest.json").read_text())
DEST = "/u01/user/models/pi05-base-" + MANIFEST["revision_manifest_sha256"][:16]
SSH = ["ssh", "-oBatchMode=yes", "-oStrictHostKeyChecking=yes", "-oHostKeyAlgorithms=ssh-ed25519", "h20-train"]


def remote(code):
    return subprocess.run(SSH + ["python3 -B -"], input=code, text=True,
                          check=True, capture_output=True).stdout


def main():
    print(remote(f'''import os,pathlib,shutil
assert os.getuid()==1004
base=pathlib.Path('/u01/user')
assert base.stat().st_uid==os.getuid() and base.resolve()==base
assert shutil.disk_usage(base).free>{MANIFEST['total_bytes']}*2
dest=pathlib.Path({DEST!r})
if dest.exists():
 assert dest.resolve()==dest and dest.stat().st_uid==os.getuid()
else:
 dest.mkdir(parents=True,mode=0o750)
print('TARGET_READY',dest)
'''), flush=True)
    started = time.monotonic()
    # Independent files only: each pathname belongs to exactly one TCP stream.
    groups = [[], [], []]
    totals = [0, 0, 0]
    for item in sorted(MANIFEST["files"], key=lambda value: value["size"], reverse=True):
        group = min(range(len(groups)), key=lambda index: totals[index])
        groups[group].append(item)
        totals[group] += item["size"]
    assert len({item["path"] for group in groups for item in group}) == len(MANIFEST["files"])

    def transfer_group(index):
        with tempfile.NamedTemporaryFile("w", prefix=f"h20-pi05-files-{index}-", delete=True) as entries:
            entries.write("".join(item["path"] + "\n" for item in groups[index]))
            entries.flush()
            subprocess.run(["rsync", "-rlt", "--checksum", "--partial", "--partial-dir=.rsync-partial",
                            "--chmod=D750,F640", "--info=stats2", "--files-from=" + entries.name,
                            "-e", shlex.join(SSH[:-1]), MANIFEST["source"] + "/", "h20-train:" + DEST + "/"], check=True)
        print("GROUP_COMPLETE", index, flush=True)

    print("PARALLEL_GROUP_BYTES", totals, flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(transfer_group, range(3)))
    elapsed = time.monotonic() - started
    verification = json.loads(remote(f'''import hashlib,json,pathlib,os
manifest=json.loads({json.dumps(MANIFEST)!r})
dest=pathlib.Path({DEST!r})
for item in manifest['files']:
 p=dest/item['path']; assert p.is_file() and not p.is_symlink()
 assert p.stat().st_size==item['size']
 sha=hashlib.sha256()
 with p.open('rb') as stream:
  while chunk:=stream.read(8*1024*1024):sha.update(chunk)
 assert sha.hexdigest()==item['sha256'],str(p)
mf=dest/'SOURCE_MANIFEST.json'
text=json.dumps(manifest,indent=2)+'\\n'
if mf.exists(): assert mf.read_text()==text
else: mf.write_text(text);mf.chmod(0o640)
print(json.dumps(dict(result='PASS',file_count=len(manifest['files']),total_bytes=manifest['total_bytes'],target=str(dest))))
'''))
    verification.update(checked_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        transfer_elapsed_seconds=elapsed, source=MANIFEST["source"],
                        parallel_streams=3, groups_bytes=totals,
                        revision_manifest_sha256=MANIFEST["revision_manifest_sha256"])
    with (HERE / "h20-base-verification.json").open("x") as output:
        json.dump(verification, output, indent=2)
        output.write("\n")
    print(json.dumps(verification, indent=2), flush=True)


if __name__ == "__main__":
    main()

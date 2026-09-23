#!/usr/bin/env python3
"""Development-only: record adapted hashes/diff and create source release inventory."""
from pathlib import Path
import datetime
import difflib
import hashlib
import json
ROOT=Path(__file__).resolve().parents[1]
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
    upstream=json.loads((ROOT/'UPSTREAM.json').read_text())
    patches=[]
    for row in upstream['files']:
        local=ROOT/row['path']
        row['adapted_sha256']=sha(local)
        row['modified']=row['source_sha256']!=row['adapted_sha256']
        original=Path(row['source'])
        if row['modified'] and local.suffix in ('.py','.json','.yaml','.sh') and original.is_file():
            if sha(original)!=row['source_sha256']:
                raise RuntimeError(f'upstream changed since capture: {original}')
            patches.extend(difflib.unified_diff(original.read_text().splitlines(True),local.read_text().splitlines(True),
                           fromfile='upstream/'+row['path'],tofile='adapted/'+row['path']))
    (ROOT/'UPSTREAM.json').write_text(json.dumps(upstream,indent=2)+'\n')
    (ROOT/'LOCAL_CHANGES.patch').write_text(''.join(patches))
    files=[]
    for p in sorted(ROOT.rglob('*')):
        if not p.is_file() or any(x in p.parts for x in ('__pycache__','.pytest_cache','runtime','results')):continue
        if p.name=='RELEASE_FILES.json':continue
        files.append({'path':str(p.relative_to(ROOT)),'bytes':p.stat().st_size,'sha256':sha(p)})
    (ROOT/'RELEASE_FILES.json').write_text(json.dumps({'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'files':files},indent=2)+'\n')
    print(f'release files={len(files)} bytes={sum(r["bytes"] for r in files)} modified_upstream={sum(r["modified"] for r in upstream["files"])}')
if __name__=='__main__':main()

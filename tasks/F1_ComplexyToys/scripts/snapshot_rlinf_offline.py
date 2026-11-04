#!/usr/bin/env python3
"""Read-only runtime identity/resource snapshot for an isolated benchmark."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
from urllib.request import urlopen

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--catalog',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args()
catalog=json.loads(a.catalog.read_text())
result={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'catalog_sha256':hashlib.sha256(a.catalog.read_bytes()).hexdigest(),
        'catalog_release':catalog['release'],'health':{},'files':{},'robot_commands':0}
for name,model in catalog['models'].items():
    machine=catalog['machines'][model['machine']]
    with urlopen(f'http://127.0.0.1:{model["remote_port"]}/health',timeout=5) as response:
        health=json.load(response)
    assert health['model_id']==model['model_id'] and health['model_version']==model['model_version'],name
    result['health'][name]=health
    for file,digest in machine['files_sha256'].items():
        if file not in result['files']:
            actual=hashlib.sha256(Path(file).read_bytes()).hexdigest()
            result['files'][file]={'sha256':actual,'expected':digest,'matches':actual==digest}
            assert actual==digest,file
for key,command in {
 'gpu':['nvidia-smi','--query-gpu=index,uuid,name,memory.used,memory.free,utilization.gpu','--format=csv,noheader'],
 'processes':['nvidia-smi','--query-compute-apps=pid,gpu_uuid,used_memory,process_name','--format=csv,noheader'],
 'space':['df','-h','/u01'],
}.items():
    result[key]=subprocess.check_output(command,text=True).splitlines()
result['process_owners']={}
for row in result['processes']:
    pid=row.split(',')[0].strip()
    proc=Path('/proc')/pid
    result['process_owners'][pid]={'uid':proc.stat().st_uid,
       'cmdline':(proc/'cmdline').read_bytes().replace(b'\0',b' ').decode()}
a.output.parent.mkdir(parents=True,exist_ok=True)
with a.output.open('x') as f:json.dump(result,f,indent=2)
print(json.dumps({'health_checked':len(result['health']),'files_checked':len(result['files']),'gpu':result['gpu']}))

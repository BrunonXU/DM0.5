"""Fetch one stopped, identity-bound owner recording over the existing SSH path."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import re
import shlex
import subprocess
import tarfile


REMOTE_RUNTIME = '/home/user/Bruno/HARNESS/runtime'
CONTROL_LOCK = '/home/user/Bruno/F1/runtime/control.lock'

# Sent as Python source on SSH stdin. It has no ROS/control imports or write calls.
REMOTE_READER = r'''
import fcntl, hashlib, io, json, os, re, stat, sys, tarfile, time, urllib.request
from pathlib import Path
request=json.loads(sys.argv[1])
seal_only=request.pop('_seal_only',False)
sealed_sha=request.pop('_sealed_sha256',None)
background=request.pop('_background',False)
metadata_only=request.pop('_metadata_only',False)
root=Path(request['owner_run_path']);lock=Path(request['control_lock'])
seal_path=root/'harness-export-seal.json'
if any(p.is_symlink() for p in (root,*root.parents,lock,seal_path)):
    raise ValueError('symlink_source_or_lock_forbidden')
def build_metadata():
    trace=root/'harness/trace.jsonl'
    found=False;required_blobs=set()
    def referenced(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key in ('blob_sha256','camera_info_sha256') and isinstance(item,str) and re.fullmatch('[a-f0-9]{64}',item):
                    required_blobs.add(item)
                else: referenced(item)
        elif isinstance(value,list):
            for item in value:referenced(item)
    with trace.open() as stream:
        for line in stream:
            row=json.loads(line)
            referenced(row)
            if (row.get('kind')=='task_prepared'
                    and row.get('owner_instance_id')==request['owner_instance_id']
                    and row.get('session_id')==request['session_id']
                    and row.get('task_hash')==request['task_hash']): found=True
    if not found: raise ValueError('remote_owner_task_identity_mismatch')
    def sha(path):
        h=hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''): h.update(block)
        return h.hexdigest()
    inventory={};total=0
    folders={'journal','journal.partial','payload','payload.partial','harness'}
    files={'preflight.json','ik_warmup.json','failure.txt','camera_diagnostics.json'}
    for path in sorted(root.rglob('*')):
        relative=path.relative_to(root)
        if relative.parts[0] not in folders and relative.as_posix() not in files:
            continue
        mode=path.lstat().st_mode
        if stat.S_ISLNK(mode): raise ValueError('remote_evidence_symlink_forbidden')
        if stat.S_ISDIR(mode): continue
        if not stat.S_ISREG(mode): raise ValueError('remote_evidence_special_file_forbidden')
        size=path.stat().st_size;total+=size
        if total>request['max_bytes'] or len(inventory)>=20000:
            raise ValueError('remote_evidence_budget_exceeded')
        inventory[path.relative_to(root).as_posix()]=dict(bytes=size,sha256=sha(path))
    sources={relative:root/relative for relative in inventory};virtual={}
    task_id=request.get('task_id','');missing=[];collected=[];diagnostics=[];diagnostic_skips=[]
    if re.fullmatch('web-[a-f0-9]{32}',task_id):
        blob_root=Path('/home/user/Bruno/HARNESS/runs')/('services-'+task_id)/'processes/gateway/blobs'
        for digest in sorted(required_blobs):
            path=blob_root/digest
            if any(p.is_symlink() for p in (path,*path.parents)):
                raise ValueError('scene_evidence_symlink_forbidden')
            if not path.is_file():missing.append(digest);continue
            size=path.stat().st_size
            if total+size>request['max_bytes'] or len(inventory)>=19999:
                missing.append(digest);continue
            if sha(path)!=digest:raise ValueError('scene_blob_hash_mismatch')
            relative='scene/blobs/'+digest
            inventory[relative]=dict(bytes=size,sha256=digest);sources[relative]=path
            total+=size;collected.append(digest)
        # Optional diagnostics never take space away from owner-referenced
        # evidence. Only immutable, completed snapshots of this exact task and
        # owner are included; a background writer may still be finishing others.
        failure_root=blob_root.parent/'scene-failures'
        manifest_reserve=len(json.dumps(dict(required=sorted(required_blobs),collected=collected,missing=missing)).encode())+16384
        if not any(p.is_symlink() for p in (failure_root,*failure_root.parents)) and failure_root.is_dir():
            for path in sorted(failure_root.glob('*.json'))[:16]:
                if not re.fullmatch('[a-f0-9]{32}\\.json',path.name):continue
                try:
                    if path.is_symlink() or not path.is_file() or path.stat().st_size>256*1024:
                        raise ValueError('invalid_diagnostic_file')
                    payload=path.read_bytes();record=json.loads(payload)
                    context=record.get('context',{})
                    if (record.get('schema')!='f1-scene-failure-v1' or record.get('diagnostic_only') is not True
                            or record.get('snapshot_id')!=path.stem
                            or any(context.get(k)!=request[k] for k in ('owner_instance_id','session_id','task_id','task_hash'))):
                        raise ValueError('diagnostic_scope_mismatch')
                    refs=record.get('persisted_blobs',[])
                    if (not isinstance(refs,list) or len(refs)>32
                            or any(not isinstance(h,str) or not re.fullmatch('[a-f0-9]{64}',h) for h in refs)):
                        raise ValueError('invalid_diagnostic_blob_list')
                    if total+len(payload)+manifest_reserve>request['max_bytes'] or len(inventory)>=19999:
                        raise ValueError('diagnostic_export_budget')
                    relative='scene/failures/'+path.name
                    inventory[relative]=dict(bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest())
                    sources[relative]=path;total+=len(payload)
                    omitted=[]
                    for h in sorted(set(refs)):
                        key='scene/failures/blobs/'+h
                        if key in inventory:continue
                        source=failure_root/'blobs'/h
                        if source.parent.is_symlink() or source.is_symlink() or not source.is_file():omitted.append(h);continue
                        size=source.stat().st_size
                        if total+size+manifest_reserve>request['max_bytes'] or len(inventory)>=19999:
                            omitted.append(h);continue
                        if sha(source)!=h:omitted.append(h);continue
                        inventory[key]=dict(bytes=size,sha256=h);sources[key]=source;total+=size
                    diagnostics.append(dict(path=relative,capture_complete=record.get('complete') is True,
                        missing_export_blobs=omitted,export_complete=not omitted))
                except (ValueError,OSError,TypeError,AttributeError) as exc:
                    diagnostic_skips.append(dict(file=path.name,reason=str(exc)[:160]))
    else:missing=sorted(required_blobs)
    scene=dict(schema='f1-harness-used-scenes-v1',task_id=task_id,required=sorted(required_blobs),
        collected=collected,missing=missing,complete=not missing,scope='owner_referenced_rgbd_and_intrinsics')
    scene['diagnostics']=dict(snapshots=diagnostics,skipped=diagnostic_skips,
        scope='completed_matching_snapshots_at_seal; not a continuous or exhaustive capture')
    value=json.dumps(scene,sort_keys=True).encode();relative='scene/manifest.json'
    if total+len(value)>request['max_bytes']:raise ValueError('collection_budget_exceeded')
    virtual[relative]=value;inventory[relative]=dict(bytes=len(value),sha256=hashlib.sha256(value).hexdigest());total+=len(value)
    metadata=dict(schema='f1-harness-owner-collection-v1',request=request,
        source_quiescence='shared_nonblocking_existing_control_lock',files=inventory,
        total_bytes=total,command_publishers=0)
    metadata['scene_manifest']=scene
    return metadata

def encode(value):return json.dumps(value,sort_keys=True).encode()
if sealed_sha:
    raw=seal_path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=sealed_sha:raise ValueError('export_seal_changed')
    metadata=json.loads(raw)
    if metadata['request']!=request:raise ValueError('export_seal_request_mismatch')
else:
    with lock.open('rb') as control:
        try:fcntl.flock(control,fcntl.LOCK_SH|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('owner_or_other_controller_still_running')
        if seal_path.exists():
            metadata=json.loads(seal_path.read_bytes())
            if metadata['request']!=request:raise ValueError('export_seal_request_mismatch')
        else:
            metadata=build_metadata()
            temporary=seal_path.with_suffix('.tmp-'+str(os.getpid()))
            with temporary.open('xb') as out:
                out.write(encode(metadata));out.flush();os.fsync(out.fileno())
            try:os.link(temporary,seal_path)
            except FileExistsError:
                if json.loads(seal_path.read_bytes())!=metadata:raise ValueError('export_seal_conflict')
            finally:temporary.unlink()
        raw=seal_path.read_bytes()
        fcntl.flock(control,fcntl.LOCK_UN)
if seal_only:
    print(json.dumps(dict(schema='f1-harness-export-receipt-v1',request=request,
        seal_sha256=hashlib.sha256(raw).hexdigest(),files=len(metadata['files']),
        total_bytes=metadata['total_bytes'],command_publishers=0)))
    sys.exit(0)
if metadata_only:
    # The robot owns durable originals; background review only needs logs and
    # indices. Video rendering has one owner: the existing preview worker.
    originals=metadata['files']
    selected={name:item for name,item in originals.items()
              if not name.endswith('.pack') and '/blobs/' not in name}
    total=sum(item['bytes'] for item in selected.values())
    # Journals stream to disk; use the already sealed run budget, not a second
    # 64 MB cap which rejected ordinary multi-minute control recordings.
    if total>request['max_bytes']:raise ValueError('metadata_collection_budget_exceeded')
    metadata=dict(metadata,files=selected,total_bytes=total,
                  remote_files=originals,storage_policy='f1_primary_metadata_local',
                  local_complete=False,export_seal_sha256=hashlib.sha256(raw).hexdigest())
# Copy only the already closed, identity-bound run; no control lock is held.
# Hashes are checked on the receiver and again here; a changed source fails export.
virtual={'scene/manifest.json':encode(metadata['scene_manifest'])}
blob_root=Path('/home/user/Bruno/HARNESS/runs')/('services-'+request.get('task_id',''))/'processes/gateway/blobs'
class BackgroundOutput:
    def __init__(self):self.checked=0.;self.deadline=0.
    def write(self,data):
        if background:
            while time.monotonic()-self.checked>=.25:
                try:
                    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open('http://127.0.0.1:4242/api/background/activity',timeout=2) as response:
                        state=json.load(response)
                    if state.get('ok') is True and state.get('busy') is False:
                        self.checked=time.monotonic();break
                except (OSError,ValueError):pass
                time.sleep(1)
            # Yield bandwidth as well as control ownership; no control lock held.
            wait=self.deadline-time.monotonic()
            if wait>0:time.sleep(wait)
            self.deadline=time.monotonic()+len(data)/(2*1024*1024)
        return sys.stdout.buffer.write(data)
    def flush(self):sys.stdout.buffer.flush()
with tarfile.open(fileobj=BackgroundOutput(),mode='w|') as archive:
    payload=encode(metadata);entry=tarfile.TarInfo('collection.json');entry.size=len(payload)
    archive.addfile(entry,io.BytesIO(payload))
    for relative,item in metadata['files'].items():
        entry=tarfile.TarInfo('owner/'+relative);entry.size=item['bytes'];entry.mode=0o600
        if relative in virtual:
            archive.addfile(entry,io.BytesIO(virtual[relative]));continue
        if relative.startswith('scene/blobs/'):path=blob_root/relative.rsplit('/',1)[-1]
        elif relative.startswith('scene/failures/'):
            path=blob_root.parent/'scene-failures'/relative.removeprefix('scene/failures/')
        else:path=root/relative
        if any(p.is_symlink() for p in (path,*path.parents)) or not path.is_file():
            raise ValueError('export_source_not_regular')
        if path.stat().st_size!=item['bytes']:raise ValueError('remote_evidence_changed_during_collection')
        h=hashlib.sha256()
        with path.open('rb') as stream:archive.addfile(entry,stream)
        with path.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
        if path.stat().st_size!=item['bytes'] or h.hexdigest()!=item['sha256']:
            raise ValueError('remote_evidence_changed_during_collection')
'''


def bound_owner(planner_run, *, remote_runtime=REMOTE_RUNTIME, max_bytes=16 * 1024**3):
    """Use the task's original binding, never a mutable last-run pointer."""
    from .contracts import digest
    runtime = PurePosixPath(remote_runtime)
    allowed = PurePosixPath('/home/user/Bruno/HARNESS')
    if not runtime.is_relative_to(allowed) or '..' in runtime.parts:
        raise ValueError('remote_runtime_outside_harness')
    if type(max_bytes) is not int or not 0 < max_bytes <= 64 * 1024**3:
        raise ValueError('collection_budget_out_of_bounds')
    rows = []
    with (Path(planner_run) / 'trace.jsonl').open() as stream:
        for line in stream:
            row = json.loads(line)
            if row.get('kind') == 'remote_task_prepared':
                rows.append(row)
    if len(rows) != 1:
        raise ValueError('one_bound_prepared_task_required')
    row = rows[0]
    identity = row.get('recording_identity') or {}
    run_id = identity.get('owner_run_id', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', run_id):
        raise ValueError('invalid_owner_run_id')
    expected = runtime / 'harness-runs' / run_id
    if identity.get('owner_run_path') != str(expected):
        raise ValueError('owner_run_path_outside_bound_runtime')
    if (not row.get('owner_instance_id') or not row.get('session_id')
            or identity.get('owner_instance_id') != row['owner_instance_id']):
        raise ValueError('owner_instance_binding_missing')
    return dict(owner_run_path=str(expected), owner_run_id=run_id,
                owner_instance_id=row['owner_instance_id'], session_id=row['session_id'],
                task_hash=digest(row['task']), task_id=row['task']['task_id'], control_lock=CONTROL_LOCK, max_bytes=max_bytes)


def ssh_reader_command(request, *, host, port, identity, known_hosts):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+', host) or host.startswith('-'):
        raise ValueError('explicit_ssh_user_and_host_required')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('invalid_ssh_port')
    return ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=15',
            '-o', 'ServerAliveCountMax=3', '-o', 'UserKnownHostsFile=' + str(Path(known_hosts).resolve()),
            '-i', str(Path(identity).resolve()), '-p', str(port), host,
            shlex.join(['python3', '-', json.dumps(request, separators=(',', ':'))])]


def extract_collection(archive_path, directory, request):
    """Extract regular members only, with exact inventory and content validation."""
    root = Path(directory)
    seen = set()
    with tarfile.open(archive_path, 'r:') as archive:
        members = archive.getmembers()
        if not members or members[0].name != 'collection.json' or members[0].size > 8 * 1024**2:
            raise ValueError('collection_metadata_missing_or_oversized')
        if not members[0].isfile():
            raise ValueError('collection_metadata_not_regular')
        metadata = json.load(archive.extractfile(members[0]))
        if metadata.get('schema') != 'f1-harness-owner-collection-v1' or metadata.get('request') != request:
            raise ValueError('collection_request_mismatch')
        inventory = metadata.get('files')
        if not isinstance(inventory, dict) or len(inventory) > 20000:
            raise ValueError('collection_inventory_invalid')
        total = 0
        for member in members[1:]:
            relative = PurePosixPath(member.name)
            if (not member.isfile() or relative.is_absolute() or '..' in relative.parts
                    or len(relative.parts) < 2 or relative.parts[0] != 'owner'
                    or member.name in seen):
                raise ValueError('unsafe_or_duplicate_collection_member')
            seen.add(member.name)
            key = relative.relative_to('owner').as_posix()
            item = inventory.get(key)
            if not isinstance(item, dict) or member.size != item.get('bytes'):
                raise ValueError('collection_inventory_mismatch')
            total += member.size
            if total > request['max_bytes']:
                raise ValueError('collection_budget_exceeded')
            target = root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            h = hashlib.sha256()
            with target.open('xb') as output, archive.extractfile(member) as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    h.update(chunk)
                    output.write(chunk)
            if h.hexdigest() != item.get('sha256'):
                raise ValueError('collection_content_hash_mismatch')
        if seen != {'owner/' + key for key in inventory} or total != metadata.get('total_bytes'):
            raise ValueError('collection_inventory_incomplete')
    (root / 'collection.json').write_text(json.dumps(metadata, indent=2) + '\n')
    return metadata


def prepare_collection(planner_run, *, host, port, identity, known_hosts,
                       remote_runtime=REMOTE_RUNTIME, max_bytes=16 * 1024**3):
    request = bound_owner(planner_run, remote_runtime=remote_runtime, max_bytes=max_bytes)
    command = ssh_reader_command(dict(request, _seal_only=True), host=host, port=port,
                                 identity=identity, known_hosts=known_hosts)
    result = subprocess.run(command, input=REMOTE_READER, text=True, capture_output=True,
                            timeout=120, check=True)
    receipt = json.loads(result.stdout)
    if (receipt.get('request') != request or receipt.get('schema') != 'f1-harness-export-receipt-v1'
            or not re.fullmatch('[a-f0-9]{64}', receipt.get('seal_sha256', ''))):
        raise ValueError('invalid_export_seal_receipt')
    return receipt


def collect_rollout(planner_run, output, *, host, port, identity, known_hosts,
                    remote_runtime=REMOTE_RUNTIME, max_bytes=16 * 1024**3, seal=False,
                    render_video=False, sealed_receipt=None, background=False, metadata_only=False):
    request = bound_owner(planner_run, remote_runtime=remote_runtime, max_bytes=max_bytes)
    output = Path(output).resolve()
    stage = output.with_name(output.name + '.partial')
    if output.exists() or stage.exists():
        raise FileExistsError('collection_destination_must_be_new')
    remote_request = dict(request)
    if background:remote_request['_background']=True
    if metadata_only:
        if seal or render_video:raise ValueError('metadata_only_cannot_render_or_seal_local_payload')
        remote_request['_metadata_only']=True
    if sealed_receipt is not None:
        if sealed_receipt.get('request') != request:
            raise ValueError('export_receipt_task_mismatch')
        remote_request['_sealed_sha256'] = sealed_receipt['seal_sha256']
    command = ssh_reader_command(remote_request, host=host, port=port, identity=identity, known_hosts=known_hosts)
    stage.mkdir(parents=True)
    archive = stage / 'owner.tar'
    try:
        with archive.open('xb') as stream:
            process = subprocess.run(command, input=REMOTE_READER.encode(), stdout=stream,
                                     stderr=subprocess.PIPE, timeout=600, check=False)
        (stage / 'transfer.stderr').write_bytes(process.stderr)
        if process.returncode:
            raise RuntimeError('owner_readonly_collection_failed:' + str(process.returncode))
        metadata = extract_collection(archive, stage, request)
        archive.unlink()  # Validated copy is retained, not a second raw payload.
        stage.rename(output)
    except Exception as exc:
        (stage / 'failure.json').write_text(json.dumps(dict(error=type(exc).__name__, reason=str(exc))) + '\n')
        raise
    result = dict(output=str(output), owner_run=str(output / 'owner'),
                  files=len(metadata['files']), bytes=metadata['total_bytes'],
                  collected=True, command_publishers=0, training_ready=False)
    if metadata_only:
        if metadata.get('storage_policy')!='f1_primary_metadata_local':
            raise ValueError('metadata_only_policy_not_acknowledged')
        result.update(storage_policy='f1_primary_metadata_local',local_complete=False,
                      remote_owner_run=request['owner_run_path'],
                      remote_files=len(metadata['remote_files']),
                      export_seal_sha256=metadata['export_seal_sha256'])
        (output/'availability.json').write_text(json.dumps(dict(
            schema='f1-remote-availability-v1',local_complete=False,
            owner_run=request['owner_run_path'],task=request,
            files=metadata['remote_files'],export_seal_sha256=metadata['export_seal_sha256']),indent=2)+'\n')
    if seal or render_video:
        from .evidence_pack import seal_run
        try:
            result['seal'] = seal_run(planner_run, output / 'owner', output / 'sealed', render_video=render_video)
        except Exception as exc:
            result['seal'] = dict(integrity_complete=False, autonomous_success=False,
                                  error=type(exc).__name__, reason=str(exc))
    (output / 'result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    return result

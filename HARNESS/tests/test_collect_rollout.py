import fcntl
import hashlib
import io
import json
import subprocess
import sys
import tarfile

import pytest

from f1_harness.collect import (REMOTE_READER, bound_owner, collect_rollout,
                               extract_collection, ssh_reader_command)


def planner(tmp_path):
    root = tmp_path / 'planner'; root.mkdir()
    identity = dict(owner_run_id='f1_hil_example', owner_instance_id='owner-1',
        owner_run_path='/home/user/Bruno/HARNESS/runtime/harness-runs/f1_hil_example', host_boot_id='boot-1')
    row = dict(kind='remote_task_prepared', owner_instance_id='owner-1', session_id='session-1',
               task={'task_id': 'square'}, recording_identity=identity)
    (root / 'trace.jsonl').write_text(json.dumps(row) + '\n')
    return root, row


def archive(path, request, *, member='owner/harness/trace.jsonl', content=b'{}\n', digest=None):
    key = member.removeprefix('owner/')
    metadata = dict(schema='f1-harness-owner-collection-v1', request=request,
        files={key: dict(bytes=len(content), sha256=digest or hashlib.sha256(content).hexdigest())},
        total_bytes=len(content))
    with tarfile.open(path, 'w') as stream:
        for name, data in [('collection.json', json.dumps(metadata).encode()), (member, content)]:
            item = tarfile.TarInfo(name); item.size = len(data)
            stream.addfile(item, io.BytesIO(data))


def test_collect_uses_prepared_run_instead_of_latest(tmp_path):
    root, _ = planner(tmp_path)
    request = bound_owner(root)
    assert request['owner_run_id'] == 'f1_hil_example'
    assert request['session_id'] == 'session-1'


@pytest.mark.parametrize('field,value', [
    ('owner_run_path', '/home/user/Bruno/F1/runtime/private'),
    ('owner_run_path', '/home/user/Bruno/HARNESS/runtime/harness-runs/../private'),
    ('owner_run_id', '../../private'), ('owner_instance_id', 'different-owner')])
def test_collect_rejects_unbound_identity(tmp_path, field, value):
    root, row = planner(tmp_path)
    row['recording_identity'][field] = value
    (root / 'trace.jsonl').write_text(json.dumps(row) + '\n')
    with pytest.raises(ValueError):
        bound_owner(root)


def test_collect_rejects_multiple_tasks_and_runtime_escape(tmp_path):
    root, row = planner(tmp_path)
    with pytest.raises(ValueError, match='remote_runtime_outside_harness'):
        bound_owner(root, remote_runtime='/etc')
    (root / 'trace.jsonl').write_text((json.dumps(row) + '\n') * 2)
    with pytest.raises(ValueError, match='one_bound_prepared_task'):
        bound_owner(root)


def test_ssh_options_are_explicit_and_remote_arguments_quoted(tmp_path):
    root, _ = planner(tmp_path)
    request = bound_owner(root)
    command = ssh_reader_command(request, host='user@127.0.0.1', port=22009,
                                 identity=tmp_path / 'key', known_hosts=tmp_path / 'known_hosts')
    assert 'BatchMode=yes' in command and 'StrictHostKeyChecking=yes' in command
    import shlex
    assert json.loads(shlex.split(command[-1])[2]) == request
    with pytest.raises(ValueError, match='explicit_ssh_user'):
        ssh_reader_command(request, host='-oProxyCommand=bad', port=22, identity='key', known_hosts='known')


@pytest.mark.parametrize('member', ['owner/../../escape', '/etc/shadow', 'different/file'])
def test_archive_cannot_escape_owner_tree(tmp_path, member):
    request = {'max_bytes': 100}
    path = tmp_path / 'archive.tar'; archive(path, request, member=member)
    with pytest.raises(ValueError, match='unsafe_or_duplicate'):
        extract_collection(path, tmp_path / 'out', request)
    assert not (tmp_path / 'escape').exists()


def test_archive_hash_mismatch_is_rejected(tmp_path):
    request = {'max_bytes': 100}
    path = tmp_path / 'archive.tar'; archive(path, request, digest='0' * 64)
    with pytest.raises(ValueError, match='content_hash_mismatch'):
        extract_collection(path, tmp_path / 'out', request)


def test_collect_keeps_existing_outputs_and_failed_partial(tmp_path, monkeypatch):
    root, _ = planner(tmp_path)
    kwargs = dict(host='user@127.0.0.1', port=22009, identity='key', known_hosts='known')
    def failed(*args, **kw):
        return subprocess.CompletedProcess(args[0], 1, stderr=b'owner still running')
    monkeypatch.setattr('f1_harness.collect.subprocess.run', failed)
    with pytest.raises(RuntimeError, match='collection_failed'):
        collect_rollout(root, tmp_path / 'collection', **kwargs)
    assert (tmp_path / 'collection.partial/failure.json').is_file()
    with pytest.raises(FileExistsError):
        collect_rollout(root, tmp_path / 'collection', **kwargs)


def test_collect_calls_existing_sealer_after_validated_transfer(tmp_path, monkeypatch):
    root, _ = planner(tmp_path); request = bound_owner(root)
    tar = tmp_path / 'source.tar'; archive(tar, request)
    def fetch(*args, **kw):
        kw['stdout'].write(tar.read_bytes())
        assert kw['input'] == REMOTE_READER.encode()
        return subprocess.CompletedProcess(args[0], 0, stderr=b'')
    monkeypatch.setattr('f1_harness.collect.subprocess.run', fetch)
    calls = []
    def seal(p, o, target, **kwargs):
        calls.append((p, o, target))
        assert (o / 'harness/trace.jsonl').read_bytes() == b'{}\n'
        return {'integrity_complete': False, 'autonomous_success': False}
    monkeypatch.setattr('f1_harness.evidence_pack.seal_run', seal)
    result = collect_rollout(root, tmp_path / 'collected', host='user@127.0.0.1', port=22009,
        identity='key', known_hosts='known', seal=True)
    assert result['collected'] and result['command_publishers'] == 0
    assert not result['seal']['autonomous_success'] and len(calls) == 1
    assert not (tmp_path / 'collected/owner.tar').exists()


def test_remote_reader_rejects_active_control_lock_and_copies_stopped_failure(tmp_path):
    owner = tmp_path / 'owner'; (owner / 'harness').mkdir(parents=True)
    (owner / 'harness/trace.jsonl').write_text(json.dumps(dict(kind='task_prepared',
        owner_instance_id='owner', session_id='session', task_hash='task')) + '\n')
    (owner / 'failure.txt').write_text('real startup failure')
    # Model assets, arbitrary files and credentials are outside the evidence whitelist.
    (owner / 'API.MD').write_text('must not be transferred')
    lock = tmp_path / 'control.lock'; lock.write_text('')
    request = dict(owner_run_path=str(owner), owner_instance_id='owner', session_id='session',
                   task_hash='task', control_lock=str(lock), max_bytes=10000)
    with lock.open('rb') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run([sys.executable, '-c', REMOTE_READER, json.dumps(request)], capture_output=True)
        assert result.returncode != 0 and b'still_running' in result.stderr
    result = subprocess.run([sys.executable, '-c', REMOTE_READER, json.dumps(request)], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
    tar = tmp_path / 'archive.tar'; tar.write_bytes(result.stdout)
    metadata = extract_collection(tar, tmp_path / 'collected', request)
    assert set(metadata['files']) == {'failure.txt', 'harness/trace.jsonl', 'scene/manifest.json'}
    assert (tmp_path / 'collected/owner/failure.txt').read_text() == 'real startup failure'


def test_background_metadata_keeps_identity_logs_and_remote_inventory_without_payload(tmp_path):
    owner=tmp_path/'owner';(owner/'harness').mkdir(parents=True)
    trace=owner/'harness/trace.jsonl'
    trace.write_text(json.dumps(dict(kind='task_prepared',owner_instance_id='o',session_id='s',task_hash='t'))+'\n')
    (owner/'payload').mkdir();pack=owner/'payload/head.pack';pack.write_bytes(b'raw'*10000)
    lock=tmp_path/'lock';lock.touch()
    request=dict(owner_run_path=str(owner),owner_instance_id='o',session_id='s',task_hash='t',control_lock=str(lock),max_bytes=100000)
    result=subprocess.run([sys.executable,'-c',REMOTE_READER,json.dumps(request|{'_metadata_only':True})],capture_output=True)
    assert result.returncode==0,result.stderr.decode()
    archive=tmp_path/'archive.tar';archive.write_bytes(result.stdout)
    metadata=extract_collection(archive,tmp_path/'local',request)
    assert metadata['local_complete'] is False
    assert metadata['remote_files']['payload/head.pack']['sha256']==hashlib.sha256(pack.read_bytes()).hexdigest()
    assert not (tmp_path/'local/owner/payload/head.pack').exists()
    assert (tmp_path/'local/owner/harness/trace.jsonl').read_bytes()==trace.read_bytes()
    assert len(result.stdout)<pack.stat().st_size


def test_sealed_copy_works_during_next_controller_and_rejects_source_changes(tmp_path):
    owner=tmp_path/'owner';(owner/'harness').mkdir(parents=True)
    (owner/'harness/trace.jsonl').write_text(json.dumps(dict(kind='task_prepared',
        owner_instance_id='owner',session_id='session',task_hash='task'))+'\n')
    failure=owner/'failure.txt';failure.write_text('original failure')
    lock=tmp_path/'control.lock';lock.touch()
    request=dict(owner_run_path=str(owner),owner_instance_id='owner',session_id='session',
                 task_hash='task',control_lock=str(lock),max_bytes=10000)
    def read(extra):
        return subprocess.run([sys.executable,'-c',REMOTE_READER,json.dumps(request|extra)],capture_output=True)
    seal=read({'_seal_only':True});assert seal.returncode==0,seal.stderr
    receipt=json.loads(seal.stdout)
    with lock.open('rb') as control:
        fcntl.flock(control,fcntl.LOCK_EX|fcntl.LOCK_NB)
        result=read({'_sealed_sha256':receipt['seal_sha256']})
        assert result.returncode==0,result.stderr
        path=tmp_path/'closed.tar';path.write_bytes(result.stdout)
        extract_collection(path,tmp_path/'verified',request)
        failure.write_text('tampered source')
        rejected=read({'_sealed_sha256':receipt['seal_sha256']})
        assert rejected.returncode!=0 and b'remote_evidence_changed' in rejected.stderr


def test_zero_candidate_failure_exports_planning_rgbd_without_revalidation_event(tmp_path):
    task_id='web-'+'a'*32
    owner=tmp_path/'owner';(owner/'harness').mkdir(parents=True)
    root=tmp_path/'runs';blobs=root/('services-'+task_id)/'processes/gateway/blobs';blobs.mkdir(parents=True)
    hashes=[]
    for data in (b'original rgb',b'original depth',b'camera info'):
        sha=hashlib.sha256(data).hexdigest();hashes.append(sha);(blobs/sha).write_bytes(data)
    events=[dict(kind='task_prepared',owner_instance_id='owner',session_id='session',task_hash='task'),
        dict(kind='harness_planning_started',observation={'frames':[
            {'blob_sha256':hashes[0],'camera_info_sha256':hashes[2]},
            {'blob_sha256':hashes[1],'camera_info_sha256':hashes[2]}]}),
        dict(kind='harness_skill_computation_failed',reason='registered_candidates_exhausted')]
    (owner/'harness/trace.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
    lock=tmp_path/'control.lock';lock.touch()
    request=dict(owner_run_path=str(owner),owner_instance_id='owner',session_id='session',
        task_hash='task',task_id=task_id,control_lock=str(lock),max_bytes=10000)
    reader=REMOTE_READER.replace("Path('/home/user/Bruno/HARNESS/runs')",'Path('+repr(str(root))+')')
    result=subprocess.run([sys.executable,'-c',reader,json.dumps(request)],capture_output=True)
    assert result.returncode==0,result.stderr
    tar=tmp_path/'archive.tar';tar.write_bytes(result.stdout)
    extract_collection(tar,tmp_path/'collected',request)
    report=json.loads((tmp_path/'collected/owner/scene/manifest.json').read_text())
    assert report['complete'] and report['required']==sorted(hashes)
    assert report['collected']==sorted(hashes)

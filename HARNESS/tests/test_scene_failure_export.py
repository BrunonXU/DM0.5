import hashlib
import json
import subprocess
import sys

import pytest

from f1_harness.collect import REMOTE_READER, extract_collection
from f1_harness.evidence import EvidenceStore, FrameCache
from f1_harness.scene_failures import SceneFailureRecorder
from test_scene_failure_capture import inputs


def setup(tmp_path, *, change=None, max_bytes=1024*1024):
    task_id='web-'+'a'*32;owner=tmp_path/'owner';(owner/'harness').mkdir(parents=True)
    root=tmp_path/'runs';gateway=root/('services-'+task_id)/'processes/gateway'
    store=EvidenceStore(gateway);cache=FrameCache();snapshot=inputs(cache)
    if change is not None:snapshot['context'].update(change)
    writer=SceneFailureRecorder(store);assert writer.offer('lost_target',snapshot,cache.blob)
    assert not writer.close()['writer_alive']
    (owner/'harness/trace.jsonl').write_text(json.dumps(dict(kind='task_prepared',
        owner_instance_id='owner',session_id='session',task_hash='hash'))+'\n')
    lock=tmp_path/'control.lock';lock.touch()
    request=dict(owner_run_path=str(owner),owner_instance_id='owner',session_id='session',
        task_hash='hash',task_id=task_id,control_lock=str(lock),max_bytes=max_bytes)
    reader=REMOTE_READER.replace("Path('/home/user/Bruno/HARNESS/runs')",'Path('+repr(str(root))+')')
    return request,reader,store


def export(tmp_path,request,reader):
    response=subprocess.run([sys.executable,'-c',reader,json.dumps(request)],capture_output=True)
    assert response.returncode==0,response.stderr.decode()
    path=tmp_path/'owner.tar';path.write_bytes(response.stdout)
    return extract_collection(path,tmp_path/'exported',request)


def test_rejected_scene_and_rgbd_export_even_if_owner_never_received_geometry(tmp_path):
    request,reader,store=setup(tmp_path)
    metadata=export(tmp_path,request,reader)
    scene=metadata['scene_manifest'];assert scene['required']==[]
    row=scene['diagnostics']['snapshots'][0]
    assert row['export_complete'] and row['capture_complete']
    record=json.loads((tmp_path/'exported/owner'/row['path']).read_text())
    assert record['context']['task_id']==request['task_id'] and record['reason']=='lost_target'
    for sha in record['persisted_blobs']:
        data=(tmp_path/'exported/owner/scene/failures/blobs'/sha).read_bytes()
        assert hashlib.sha256(data).hexdigest()==sha


@pytest.mark.parametrize('key',['owner_instance_id','session_id','task_id','task_hash'])
def test_other_owner_or_task_diagnostics_cannot_enter_run(tmp_path,key):
    request,reader,_=setup(tmp_path,change={key:'different'})
    scene=export(tmp_path,request,reader)['scene_manifest']
    assert not scene['diagnostics']['snapshots']
    assert scene['diagnostics']['skipped'][0]['reason']=='diagnostic_scope_mismatch'


def test_diagnostic_budget_does_not_fail_original_owner_export(tmp_path):
    request,reader,_=setup(tmp_path,max_bytes=10000)
    metadata=export(tmp_path,request,reader)
    assert metadata['scene_manifest']['complete']
    assert metadata['scene_manifest']['diagnostics']['skipped'][0]['reason']=='diagnostic_export_budget'
    assert 'harness/trace.jsonl' in metadata['files']


def test_partial_snapshot_is_ignored_and_missing_blob_is_explicit(tmp_path):
    request,reader,store=setup(tmp_path)
    root=store.root/'scene-failures'
    (root/'unfinished.partial').write_text('{')
    record=json.loads(next(root.glob('*.json')).read_text())
    (store.root/'scene-failures/blobs'/record['persisted_blobs'][0]).unlink()
    scene=export(tmp_path,request,reader)['scene_manifest']
    rows=scene['diagnostics']['snapshots']
    assert len(rows)==1 and not rows[0]['export_complete'] and len(rows[0]['missing_export_blobs'])==1
    assert scene['complete']  # No missing owner-required evidence; diagnostic lane is separate.


def test_sealed_snapshot_tamper_is_detected_on_later_copy(tmp_path):
    request,reader,store=setup(tmp_path)
    sealed=subprocess.run([sys.executable,'-c',reader,json.dumps(request|{'_seal_only':True})],capture_output=True)
    assert sealed.returncode==0,sealed.stderr.decode()
    receipt=json.loads(sealed.stdout)
    path=next((store.root/'scene-failures').glob('*.json'));path.write_text('{}')
    response=subprocess.run([sys.executable,'-c',reader,
        json.dumps(request|{'_sealed_sha256':receipt['seal_sha256']})],capture_output=True)
    assert response.returncode!=0 and b'remote_evidence_changed' in response.stderr

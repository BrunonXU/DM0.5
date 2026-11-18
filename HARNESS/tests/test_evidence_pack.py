import hashlib
import json
from pathlib import Path
import sys
import threading
import time
import subprocess
import os
from io import BytesIO

import pytest

ROOT=Path(__file__).resolve().parents[2]
for path in ('DAgger/F1/src','DAgger/shared/src','DAgger/F1/tests'):
    sys.path.insert(0,str(ROOT/path))

from f1_harness.contracts import Task,digest,SKILLS,PLACEMENT_SKILLS
from f1_harness.evidence import EvidenceStore
from f1_harness.evidence_pack import inspect_pair,seal_run
from f1_harness.reporting import summarize_run
from f1_harness.task_runner import EvidenceRemoteBackend
from f1dagger.harness_recording import recording_options,evaluation_binding
from f1dagger.recording import F1Recorder,make_sink,audit
from f1dagger.contract import CONTRACT
from f1dagger.observation import Observation
from f1dagger.contract import CAMERAS
from dagger_core.recording_observation import DeviceTimestamp,TimestampedFrame
from PIL import Image


def test_clean_service_process_reuses_registered_native_auditor(tmp_path):
    planner,owner=fixture(tmp_path)
    env={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME')}
    env['PYTHONPATH']=str(ROOT/'HARNESS/src')
    result=subprocess.run([sys.executable,'-c',
        'import json,sys; from f1_harness.evidence_pack import inspect_pair; '
        'print(json.dumps(inspect_pair(sys.argv[1],sys.argv[2])))',str(planner),str(owner)],
        env=env,text=True,capture_output=True,check=True,timeout=30)
    assert json.loads(result.stdout)['integrity_complete']


def observation(oid,start):
    stream=BytesIO();Image.new('RGB',(16,16)).save(stream,format='JPEG')
    frames=tuple(TimestampedFrame(c,oid,DeviceTimestamp(start,'ns','ros'),start+1,
                  stream.getvalue(),'image/jpeg',16,16) for c in CAMERAS)
    return Observation(oid,start,start+10,frames,(0.,)*16,(start+2,)*16,(0.,)*22)


def fixture(tmp_path,*,assisted=False,web=False,return_outcome=None):
    """Synthetic file fixture exercising real writers; never connects to a robot."""
    owner=tmp_path/'native';owner.mkdir()
    planner=tmp_path/'planner';store=EvidenceStore(planner)
    task=Task(task_id='unit-test',object_id='square',hole_id='hole',calibration_id='unit-cal',
              allowed_skills=SKILLS if return_outcome else PLACEMENT_SKILLS)
    identity=dict(owner_run_id='unit-owner',owner_run_path='/unit/native',owner_instance_id='instance',host_boot_id='unit-boot')
    binding=evaluation_binding(dict(execution_mode='powered',recording_mode='hil-record'),owner_run_id='unit-owner')
    binding.update(harness_owner_instance_id='instance',host_boot_id='unit-boot')
    recorder=F1Recorder(owner/'journal',episode_id='unit-owner',task_prompt='test',binding=binding)
    sink=make_sink(owner/'payload','hil-record')
    store.event('remote_task_prepared',task=task.model_dump(),session_id='session',owner_instance_id='instance',recording_identity=identity)
    owner_rows=[]
    def event(kind,**kw):
        row=dict(event_id='event-'+str(len(owner_rows)),kind=kind,monotonic_ns=1_000_000_000+len(owner_rows),**kw)
        owner_rows.append(row)
        store.event('owner_event',record=row,owner_instance_id='instance')
    event('task_prepared',task=task.model_dump(),task_hash=digest(task),session_id='session',owner_instance_id='instance')
    event('web_authorization' if web else 'local_authorization',epoch=1,synthetic=False,session_id='session',task_hash=digest(task),
          owner_instance_id='instance',**(dict(web_request_id='explicit-web-request',owner_run_id='unit-owner') if web else {}))
    skills=['approach_grasp','grasp','verify_grasp','inspect_in_hand','transfer_above_hole','align','insert','release','verify_placed']
    if return_outcome:skills.append("withdraw_reset")
    for i,skill in enumerate(skills):
        obs=observation(i,1_000_000_000+i*100_000_000)
        sink.submit(obs);recorder.record_timestamped_observation(obs,hil_state='policy',intervention_id=0,policy_epoch=1)
        sha=store.put_blob(obs.frames[0].payload)
        request='request-'+str(i);t=obs.capture_started_monotonic_ns-1
        pubs=[dict(topic_key='joint_command',monotonic_ns=t-1,publisher_gid='aa'*16)]
        if i==0:
            grip_stamp=t-1
            pubs.extend(dict(topic_key=key,monotonic_ns=grip_stamp,publisher_gid='aa'*16) for key in ('left_gripper_command','right_gripper_command'))
        recorder.control(now=t,state='policy',intervention=0,epoch=1,selected=[0.]*16,submitted=[0.]*16,
                         measured=[0.]*16,source='harness_skill',sequence=i+1,publication_complete=True,
                         execution_mode='powered',action_source_timestamps_ns=[t-1]*14+[grip_stamp]*2,publications=pubs,
                         policy_sampling=dict(request_id=request,task_hash=digest(task),epoch=1))
        event('skill_accepted',intent=dict(request_id=request,task_hash=digest(task),task_id=task.task_id,
             session_id='session',owner_instance_id='instance',epoch=1,skill=skill,observation_id='obs-'+str(i),calibration_id=task.calibration_id))
        if not skill.startswith('verify') and skill!='inspect_in_hand':
            event('harness_measured_waypoint',request_id=request,publication_id=str(i+1))
        event('skill_result',skill=skill,result=dict(request_id=request,outcome=return_outcome if skill=='withdraw_reset' else 'success',reason='unit-evidence',synthetic=False,assisted=assisted,evidence_ids=[sha]))
    if assisted:event('manual_intervention')
    assert sink.close();assert recorder.close()
    (owner/'harness').mkdir();(owner/'harness/trace.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in owner_rows))
    store.write_json('result.json',dict(measured=dict(outcome='pending_review' if return_outcome=='failure' else 'success',
        placement_success=True,reset_required=bool(return_outcome),reset_success=return_outcome=='success',synthetic=False,assisted=assisted)))
    assert audit(owner)['valid']
    return planner,owner


def test_harness_recording_full_and_shadow_zero():
    assert recording_options('harness','hil-record','auto')==('hil-record',True)
    assert recording_options('harness-shadow','hil-record','on')==('inference-only',False)
    with pytest.raises(ValueError):recording_options('harness','inference-only','off')


def test_seal_join_reuses_original_records_and_keeps_training_blocked(tmp_path):
    p,o=fixture(tmp_path)
    before=hashlib.sha256((o/'journal/manifest.json').read_bytes()).hexdigest()
    report=seal_run(p,o,tmp_path/'sealed',render_video=True)
    assert report['integrity_complete'] and report['autonomous_success'] and not report['training_ready']
    assert report['video_status']=='derived_review_only'
    assert (tmp_path/'sealed/video/head.mp4').stat().st_size>0
    assert hashlib.sha256((o/'journal/manifest.json').read_bytes()).hexdigest()==before
    assert summarize_run(tmp_path/'sealed')['autonomous_success']
    assert not summarize_run(p)['autonomous_success']
    with (tmp_path/'sealed/owner/journal/control.jsonl').open('a') as f:f.write('{}\n')
    with pytest.raises(ValueError,match='hash_mismatch'):summarize_run(tmp_path/'sealed')


def test_helped_success_not_autonomous(tmp_path):
    p,o=fixture(tmp_path,assisted=True)
    report=inspect_pair(p,o)
    assert report['verified_placement'] and report['assisted'] and not report['autonomous_success']


def test_bound_web_authorization_is_distinct_valid_evidence(tmp_path):
    p,o=fixture(tmp_path,web=True)
    report=inspect_pair(p,o)
    assert report['integrity_complete'] and report['autonomous_success']
    rows=[json.loads(line) for line in (o/'harness/trace.jsonl').read_text().splitlines()]
    assert any(row['kind']=='web_authorization' for row in rows)
    assert not any(row['kind']=='local_authorization' for row in rows)


def test_missing_verifier_image_downgrades_success(tmp_path):
    p,o=fixture(tmp_path)
    next((p/'blobs').iterdir()).unlink()
    report=seal_run(p,o,tmp_path/'sealed')
    assert 'verifier_image_evidence_missing' in report['issues']
    assert not report['autonomous_success'] and report['task_outcome']=='pending_review'


def test_recording_from_other_owner_is_rejected(tmp_path):
    p,o=fixture(tmp_path)
    m=json.loads((o/'journal/manifest.json').read_text());m['binding']['harness_owner_instance_id']='other'
    (o/'journal/manifest.json').write_text(json.dumps(m))
    assert 'owner_instance_mismatch' in inspect_pair(p,o)['issues']


def test_failed_partial_recording_is_preserved_but_never_success(tmp_path):
    p,o=fixture(tmp_path)
    (o/'journal').rename(o/'journal.partial')
    (o/'failure.txt').write_text('unit-test injected shutdown failure')
    report=seal_run(p,o,tmp_path/'sealed')
    assert not report['integrity_complete'] and not report['autonomous_success']
    assert (tmp_path/'sealed/owner/journal.partial/control.jsonl').is_file()
    assert (tmp_path/'sealed/owner/failure.txt').is_file()


def test_waypoint_without_actual_publication_is_not_evidence(tmp_path):
    p,o=fixture(tmp_path)
    path=o/'harness/trace.jsonl';rows=[json.loads(line) for line in path.read_text().splitlines()]
    next(r for r in rows if r['kind']=='harness_measured_waypoint')['publication_id']='unknown'
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    report=inspect_pair(p,o)
    assert 'waypoint_publication_missing' in report['issues'] and not report['autonomous_success']


def test_harness_cannot_be_promoted_by_standard_expert_review(tmp_path):
    from f1dagger.export import select_segments
    p,o=fixture(tmp_path)
    review=dict(contract=CONTRACT,manifest_sha256=audit(o)['manifest_sha256'],reviewer='unit-test',accepted_interventions=[1])
    with pytest.raises(ValueError,match='Harness evaluation'):select_segments(o,review)


def test_image_archive_does_not_block_motion_rpc(tmp_path,monkeypatch):
    from f1_harness.remote import RemoteTaskBackend
    monkeypatch.setattr(RemoteTaskBackend,'__init__',lambda *a,**k:None)
    entered=threading.Event();release=threading.Event()
    def blocked(self,sha):entered.set();release.wait(2)
    monkeypatch.setattr(EvidenceRemoteBackend,'archive_blob',blocked)
    backend=EvidenceRemoteBackend('http://127.0.0.1:1',None,EvidenceStore(tmp_path),'http://127.0.0.1:2')
    backend.queue_blob('a'*64)
    assert entered.wait(1)
    backend.queue_blob('b'*64)  # Returns even while the image HTTP worker is blocked.
    release.set();backend.close_images()


def test_corrupt_image_is_logged_without_replaying_robot_request(tmp_path,monkeypatch):
    import httpx
    from f1_harness.remote import RemoteTaskBackend
    calls=[]
    monkeypatch.setattr(RemoteTaskBackend,'__init__',lambda *a,**k:None)
    def call(self,method,**kwargs):
        calls.append(method)
        return dict(lifecycle='completed',outcome='success',evidence_ids=['a'*64])
    monkeypatch.setattr(RemoteTaskBackend,'call',call)
    backend=EvidenceRemoteBackend('http://127.0.0.1:1',None,EvidenceStore(tmp_path),'http://127.0.0.1:2')
    backend.image_client.close()
    backend.image_client=httpx.Client(base_url='http://127.0.0.1:2',transport=httpx.MockTransport(lambda request:httpx.Response(200,content=b'wrong image')))
    assert backend.call('skill_result')['outcome']=='success'
    backend.close_images()
    assert calls==['skill_result']
    rows=[json.loads(line) for line in (tmp_path/'trace.jsonl').read_text().splitlines()]
    assert rows[-1]['kind']=='image_evidence_incomplete'
    assert not list((tmp_path/'blobs').iterdir())


def test_blob_stream_has_total_deadline_and_shutdown_stops_writer(tmp_path,monkeypatch):
    import httpx
    from f1_harness.remote import RemoteTaskBackend
    monkeypatch.setattr(RemoteTaskBackend,'__init__',lambda *a,**k:None)
    backend=EvidenceRemoteBackend('http://127.0.0.1:1',None,EvidenceStore(tmp_path),'http://127.0.0.1:2')
    class Trickle(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(100):
                time.sleep(.01)
                yield b'x'
    backend.image_client.close()
    backend.image_client=httpx.Client(base_url='http://127.0.0.1:2',transport=httpx.MockTransport(
        lambda request:httpx.Response(200,stream=Trickle())))
    backend.blob_deadline_s=.03
    backend.queue_blob(hashlib.sha256(b'x'*100).hexdigest())
    backend.close_images()
    assert not backend.archive_thread.is_alive()
    rows=[json.loads(line) for line in (tmp_path/'trace.jsonl').read_text().splitlines()]
    assert rows[-1]['error']=='TimeoutError'
    before=(tmp_path/'trace.jsonl').read_bytes();time.sleep(.03)
    assert (tmp_path/'trace.jsonl').read_bytes()==before
    assert not list((tmp_path/'blobs').iterdir())


def test_correct_blob_download_is_hash_verified(tmp_path,monkeypatch):
    import httpx
    from f1_harness.remote import RemoteTaskBackend
    monkeypatch.setattr(RemoteTaskBackend,'__init__',lambda *a,**k:None)
    backend=EvidenceRemoteBackend('http://127.0.0.1:1',None,EvidenceStore(tmp_path),'http://127.0.0.1:2')
    class Stream(httpx.SyncByteStream):
        def __iter__(self):yield b'complete-image'
    backend.image_client.close()
    backend.image_client=httpx.Client(base_url='http://127.0.0.1:2',transport=httpx.MockTransport(
        lambda request:httpx.Response(200,stream=Stream())))
    sha=hashlib.sha256(b'complete-image').hexdigest()
    backend.queue_blob(sha);backend.close_images()
    assert (tmp_path/'blobs'/sha).read_bytes()==b'complete-image'
    assert not backend.archive_thread.is_alive()


def test_video_keeps_working_when_event_mirror_is_incomplete(tmp_path):
    p,o=fixture(tmp_path)
    EvidenceStore(p).event('owner_evidence_export_incomplete',reason='injected')
    result=seal_run(p,o,tmp_path/'sealed',render_video=True)
    assert not result['event_mirror_complete'] and not result['integrity_complete']
    assert result['raw_recording_complete'] and result['video_status']=='derived_review_only'
    assert not result['autonomous_success'] and not result['training_ready']


@pytest.mark.parametrize('return_outcome',['success','failure'])
def test_placement_evidence_survives_return_failure_without_full_success(tmp_path,return_outcome):
    p,o=fixture(tmp_path,return_outcome=return_outcome)
    report=inspect_pair(p,o)
    assert report['integrity_complete'] and report['verified_placement']
    assert report['reset_required']
    assert report['verified_reset'] is (return_outcome=='success')
    assert report['autonomous_success'] is (return_outcome=='success')

"""Small scene RPCs backed by the existing run EvidenceStore.

Only the gateway archives depth. The private socket directory binds its store;
references cannot name paths. RPC threads resolve immutable content before the
original observation/geometry contracts run. HTTP readers receive references
only. Existing collection follows their blob_sha256 in the owner journal.
"""
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import stat

from .contracts import (HarnessError, LocalCollisionEvidence,
                        LocalCollisionEvidenceReference, Observation, Frame, digest,
                        MeasuredGripperFeedback, MeasuredGripperReference)

MAX_EVIDENCE_BYTES=16*1024*1024
MANIFEST='.scene-evidence.json'


def _plain_path(path):
    path=Path(path).absolute()
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise HarnessError('scene_evidence_symlink_forbidden')
    return path


def bind_store(runtime,store):
    """Called once by the scene producer; no planner-controlled path is accepted."""
    runtime=_plain_path(runtime);root=_plain_path(store.root)
    if runtime.stat().st_uid!=os.getuid() or root.stat().st_uid!=os.getuid():
        raise HarnessError('scene_evidence_owner_mismatch')
    record=dict(schema='f1-scene-evidence-store-v1',root=str(root),
        store_id=hashlib.sha256(str(root).encode()).hexdigest())
    path=runtime/MANIFEST;data=json.dumps(record,sort_keys=True).encode()
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except FileExistsError:
        if _store(runtime)!=record:raise HarnessError('scene_evidence_store_changed')
    else:
        with os.fdopen(fd,'wb') as stream:stream.write(data)
    return record


def _store(runtime):
    path=_plain_path(Path(runtime)/MANIFEST)
    meta=path.stat()
    if meta.st_uid!=os.getuid() or meta.st_mode&0o077 or meta.st_size>4096:
        raise HarnessError('scene_evidence_store_identity_invalid')
    record=json.loads(path.read_bytes())
    if (set(record)!={'schema','root','store_id'}
            or record['schema']!='f1-scene-evidence-store-v1'
            or not Path(record['root']).is_absolute()
            or record['store_id']!=hashlib.sha256(record['root'].encode()).hexdigest()):
        raise HarnessError('scene_evidence_store_identity_invalid')
    root=_plain_path(record['root'])
    if root.stat().st_uid!=os.getuid():raise HarnessError('scene_evidence_owner_mismatch')
    return record


def archive_observation(observation,store,store_id,*,remaining_bytes=None):
    updates={};payloads={}
    evidence=observation.local_collision_evidence
    if evidence is not None:
        data=evidence.model_dump_json().encode();sha=hashlib.sha256(data).hexdigest()
        payloads[sha]=data
        updates['local_collision_evidence_ref']=LocalCollisionEvidenceReference(
            store_id=store_id,blob_sha256=sha,byte_count=len(data),
            observation_id=observation.observation_id,calibration_id=observation.calibration_id,
            cameras=tuple(v.camera for v in evidence.free_space_views))
    measured=observation.measured_gripper
    if measured is not None:
        data=measured.model_dump_json().encode();sha=hashlib.sha256(data).hexdigest()
        payloads[sha]=data
        updates['measured_gripper_ref']=MeasuredGripperReference(store_id=store_id,
            blob_sha256=sha,byte_count=len(data),observation_id=observation.observation_id,
            acquisition_instance=observation.acquisition_instance,host_boot_id=observation.host_boot_id,
            received_ns=observation.received_ns,state_received_ns=observation.state_received_ns,
            frames_sha256=digest([f.model_dump(mode='json') for f in observation.frames]),
            source_instance=measured.source_instance,sensor_boot_id=measured.boot_id,
            first_sequence=measured.samples[0].sequence if measured.samples else None,
            last_sequence=measured.samples[-1].sequence if measured.samples else None)
    if any(len(data)>MAX_EVIDENCE_BYTES for data in payloads.values()):
        raise HarnessError('scene_evidence_size_rejected')
    # Both payloads share the original producer budget. Check the total before
    # writing either; a failed second payload cannot silently bypass accounting.
    added=sum(len(data) for sha,data in payloads.items() if not (store.root/'blobs'/sha).exists())
    if remaining_bytes is not None and added>remaining_bytes:
        raise HarnessError('scene_evidence_quota_reached_new_run_required')
    for data in payloads.values():store.put_blob(data)
    return observation.model_copy(update=updates),added


def observation_wire(observation,*,include_measured_gripper=False):
    """Keep complete sensor payloads in the store, never truncate their samples."""
    exclude={field for field in ('local_collision_evidence','measured_gripper')
             if getattr(observation,field+'_ref') is not None}
    # The remote HTTP coordinator cannot read the robot-local store. Preserve
    # its complete CAN evidence there; private robot RPCs still use references.
    if include_measured_gripper:exclude.discard('measured_gripper')
    return observation.model_dump(mode='json',exclude=exclude)


def pack(value):
    if isinstance(value,dict):
        is_observation=value.get('schema_version')=='f1-harness-observation-v1'
        return {k:pack(v) for k,v in value.items()
            if not (is_observation and k in ('local_collision_evidence','measured_gripper')
                    and value.get(k+'_ref') is not None)}
    if isinstance(value,(tuple,list)):return [pack(v) for v in value]
    return value


def _blob_bytes(path,sha,size):
    # Stat identity keys invalidate the bounded decoded cache on replacement.
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as stream:
        meta=os.fstat(stream.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid!=os.getuid() or meta.st_size!=size:
            raise HarnessError('scene_evidence_blob_identity_invalid')
        data=stream.read(size+1)
    if len(data)!=size or hashlib.sha256(data).hexdigest()!=sha:
        raise HarnessError('scene_evidence_blob_integrity_failure')
    return data


@lru_cache(maxsize=8)
def _load(path,sha,size,mtime,ctime):
    return LocalCollisionEvidence.model_validate_json(_blob_bytes(path,sha,size))


@lru_cache(maxsize=8)
def _load_gripper(path,sha,size,mtime,ctime):
    return MeasuredGripperFeedback.model_validate_json(_blob_bytes(path,sha,size))


def _resolve_gripper(value,runtime):
    ref=MeasuredGripperReference.model_validate(value['measured_gripper_ref'])
    if any(value.get(key)!=getattr(ref,key) for key in ('observation_id','acquisition_instance',
            'host_boot_id','received_ns','state_received_ns')):
        raise HarnessError('measured_gripper_evidence_observation_binding_mismatch')
    frames=[Frame.model_validate(f).model_dump(mode='json') for f in value.get('frames',())]
    if digest(frames)!=ref.frames_sha256:
        raise HarnessError('measured_gripper_evidence_frame_binding_mismatch')
    record=_store(runtime)
    if ref.store_id!=record['store_id']:raise HarnessError('scene_evidence_store_mismatch')
    path=_plain_path(Path(record['root'])/'blobs'/ref.blob_sha256);meta=path.stat()
    measured=_load_gripper(str(path),ref.blob_sha256,ref.byte_count,meta.st_mtime_ns,meta.st_ctime_ns)
    first=measured.samples[0].sequence if measured.samples else None
    last=measured.samples[-1].sequence if measured.samples else None
    if (measured.source_instance!=ref.source_instance or measured.boot_id!=ref.sensor_boot_id
            or first!=ref.first_sequence or last!=ref.last_sequence):
        raise HarnessError('measured_gripper_evidence_source_binding_mismatch')
    if value.get('measured_gripper') is not None:
        raise HarnessError('scene_evidence_ambiguous_inline_payload')
    return dict(value,measured_gripper=measured.model_dump(mode='json'))


def resolve_observation(value,runtime):
    if value.get('measured_gripper_ref') is not None:
        value=_resolve_gripper(value,runtime)
    if value.get('local_collision_evidence_ref') is None:
        Observation.model_validate(value)
        return value
    ref=LocalCollisionEvidenceReference.model_validate(value['local_collision_evidence_ref'])
    if (value.get('observation_id')!=ref.observation_id
            or value.get('calibration_id')!=ref.calibration_id):
        raise HarnessError('scene_evidence_observation_binding_mismatch')
    record=_store(runtime)
    if ref.store_id!=record['store_id']:raise HarnessError('scene_evidence_store_mismatch')
    path=_plain_path(Path(record['root'])/'blobs'/ref.blob_sha256)
    meta=path.stat()
    evidence=_load(str(path),ref.blob_sha256,ref.byte_count,meta.st_mtime_ns,meta.st_ctime_ns)
    if (evidence.observation_id!=ref.observation_id or evidence.calibration_id!=ref.calibration_id
            or tuple(v.camera for v in evidence.free_space_views)!=ref.cameras
            or evidence.measured_at_ns!=value.get('received_ns')
            or evidence.state_received_ns!=value.get('state_received_ns')):
        raise HarnessError('scene_evidence_source_binding_mismatch')
    if value.get('local_collision_evidence') is not None:
        raise HarnessError('scene_evidence_ambiguous_inline_payload')
    obs=Observation.model_validate(dict(value,local_collision_evidence=evidence))
    frames={(f.camera,f.stream):f for f in obs.frames}
    if any(frames.get((f.camera,f.stream))!=f for f in evidence.source_frames):
        raise HarnessError('scene_evidence_frame_binding_mismatch')
    # RPC results remain ordinary JSON values: revalidation proofs also enter
    # the asynchronous journal. Conversion happens here, off the control tick.
    return dict(value,local_collision_evidence=evidence.model_dump(mode='json'))


def unpack(value,runtime):
    if isinstance(value,dict):
        if (value.get('schema_version')=='f1-harness-observation-v1'
                and (value.get('local_collision_evidence_ref') is not None
                     or value.get('measured_gripper_ref') is not None)):
            return resolve_observation(value,runtime)
        return {k:unpack(v,runtime) for k,v in value.items()}
    if isinstance(value,list):return [unpack(v,runtime) for v in value]
    return value

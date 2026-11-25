"""Bounded advisory scene diagnostics, written off the perception/control path."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import queue
import threading
import uuid
from .evidence import EvidenceStore


class SceneFailureRecorder:
    """Pin immutable cached bytes; publish metadata only after background writes.

    Failure to capture a diagnostic never changes motion admission. The caller
    supplies an already captured observation; this class has no robot interface.
    """
    def __init__(self, store, *, max_bytes=64*1024**2, max_samples=8, per_reason=2):
        if not (0 < max_bytes <= 128*1024**2 and 0 < max_samples <= 16 and 0 < per_reason <= max_samples):
            raise ValueError('invalid_scene_diagnostic_budget')
        self.store=store
        self.root=Path(store.root)/'scene-failures'
        # Separate blob namespace: diagnostic IO must never race the normal
        # scene writer's create-and-verify operations on the same hash file.
        self.archive=EvidenceStore(self.root)
        self.max_bytes,self.max_samples,self.per_reason=max_bytes,max_samples,per_reason
        self.queue=queue.Queue(maxsize=1)
        self.lock=threading.Lock();self.closed=threading.Event()
        self.counts=Counter();self.reasons=Counter();self.stages=Counter();self.stage_bytes=Counter();self.reserved_bytes=0
        self.thread=threading.Thread(target=self._run,daemon=True,name='harness-scene-diagnostics')
        self.thread.start()

    def offer_held_aperture(self,snapshot,scene,blob,*,now_ns,max_age_ns):
        """Keep one synchronized input when transport is complete but the aperture is missing."""
        try:
            with self.lock:
                if self.stages['held_aperture'] or self.closed.is_set():return False
            config=snapshot['geometry_config']
            cross_view=config.get('cross_view_aperture') or {}
            if not cross_view.get('max_pair_skew_ns'):return False
            if scene.geometry_scope not in ('coarse_object_verification','coarse_static_task'):return False
            if any(o.object_id==config['hole_id'] and o.pose is not None for o in scene.objects):return False
            target=next((o for o in scene.objects if o.object_id==config['object_id']),None)
            if not (target and target.visible and target.held is True and target.pose
                    and target.eef_object_pose and target.evidence_ids):return False
            reason=next((w for w in reversed(scene.quality_warnings)
                         if w.startswith(('aperture_unavailable:','fixture_unavailable:'))),None)
            if not reason:return False
            raw=snapshot['observation']
            if (raw['faults'] or not raw['host_boot_id'] or not raw['acquisition_instance']
                    or not scene.calibration_id or config.get('calibration_id')!=scene.calibration_id
                    or any(raw[key]!=getattr(scene,key) for key in ('host_boot_id','acquisition_instance','observation_id'))
                    or not snapshot.get('base_eef') or not snapshot.get('head_camera_pose')):return False
            frames={(f['camera'],f['stream']):f for f in raw['frames']}
            required={('head','rgb'),('head','depth'),('left','rgb'),('right','rgb'),('right','depth')}
            if (not required<=frames.keys() or len(frames)!=len(raw['frames'])
                    or any(f.model_dump()!=frames.get((f.camera,f.stream)) for f in scene.frames)
                    or any(not frames[k].get('camera_info_sha256') or frames[k].get('source_ns') is None
                           for k in required)):return False
            if not set(target.evidence_ids).issubset({f.blob_sha256 for f in scene.frames}):return False
            if frames['left','rgb']['frame_id']!='left_wrist_color_optical_frame':return False
            stamps=[frames[k]['received_ns'] for k in required]
            if (max(stamps)-min(stamps)>cross_view['max_pair_skew_ns']
                    or max(stamps)>now_ns or now_ns-min(stamps)>max_age_ns):return False
            for camera in ('right','head'):
                pair=next((p for p in raw['pairings'] if p['camera']==camera),None)
                feedback=snapshot.get('camera_feedback',{}).get(camera) or {}
                if (not pair or pair['quality']=='invalid' or feedback.get('host_boot_id')!=raw['host_boot_id']
                        or any(pair[s+'_sequence']!=frames[camera,s]['sequence'] for s in ('rgb','depth'))):return False
                view=config if camera==config['camera'] else config['hole_camera']
                if any(frames[camera,s]['camera_info_sha256']!=view[s+'_info_sha256'] for s in ('rgb','depth')):return False
            feedback=snapshot['camera_feedback']['right']
            if (snapshot.get('feedback')!=feedback or not scene.state16
                    or tuple(feedback['measured16'])!=scene.state16
                    or min(feedback['stamps16'][7:14])!=scene.state_received_ns):return False
            evidence=dict(observation_id=scene.observation_id,geometry_scope=scene.geometry_scope,motion_authorized=False,
                calibration_id=scene.calibration_id,scene_version=scene.scene_version,target=target.model_dump(),
                state_received_ns=scene.state_received_ns,state_valid_mask=scene.state_valid_mask,
                owner_frames=[f.model_dump() for f in scene.frames],matched_window_ns=max(stamps)-min(stamps))
            return self.offer(reason,dict(snapshot,held_aperture=evidence),blob,held_aperture=True)
        except Exception:
            with self.lock:self.counts['capture_errors']+=1
            return False

    def offer(self, reason, snapshot, blob, *, held_aperture=False):
        """Best-effort enqueue. No serialization or disk writes on this call."""
        try:
            if snapshot is None:return False
            context=snapshot.get('context',{})
            if (context.get('authorized') is not True
                    or not all(context.get(k) for k in ('owner_instance_id','session_id','task_id','task_hash'))):
                return False
            phase=context.get('phase') or 'observe'
            stage='held_aperture' if held_aperture else 'early' if phase in ('observe','prepared','approach_grasp') else 'later'
            # Reserve half the existing bounded archive for grasp/transport/place.
            # Startup errors cannot consume every post-motion diagnostic slot.
            sample_limit=1 if held_aperture else max(1,self.max_samples//2) if stage=='early' else self.max_samples
            byte_limit=self.max_bytes//2 if stage=='early' else self.max_bytes
            key=(context['owner_instance_id'],context['session_id'],context['task_hash'],phase,str(reason)[:256])
            def reserved_slot():
                return self.max_samples>1 and not held_aperture and not self.stages['held_aperture']
            def full():
                used=self.counts['accepted']-self.counts['held_aperture_write_failures']
                return (self.closed.is_set() or used>=self.max_samples-int(reserved_slot())
                        or self.stages[stage]>=sample_limit or not held_aperture and self.reasons[key]>=self.per_reason)
            with self.lock:
                if full():
                    self.counts['skipped_limit_or_closed']+=1;return False
                if self.queue.full():self.counts['skipped_busy']+=1;return False
            record=deepcopy(snapshot);record.update(schema='f1-scene-failure-v1',
                diagnostic_only=True,reason=str(reason)[:256],snapshot_id=uuid.uuid4().hex)
            payloads={};missing=[]
            for frame in record['observation']['frames']:
                for field in ('blob_sha256','camera_info_sha256'):
                    sha=frame.get(field)
                    if not sha or sha in payloads or sha in missing:continue
                    try:
                        value=blob(sha)
                        if not isinstance(value,bytes):raise ValueError('immutable_camera_bytes_required')
                        payloads[sha]=value
                    except Exception:missing.append(sha)
            record['missing_at_capture']=missing
            if missing and held_aperture:
                with self.lock:self.counts['held_aperture_source_missing']+=1
                return False
            size=sum(len(value) for value in payloads.values())
            # Reserve a bounded metadata allowance too. Unreferenced partial
            # writes count against the budget even after a writer error.
            size+=256*1024
            with self.lock:
                available_bytes=self.max_bytes-(self.max_bytes//4 if reserved_slot() else 0)
                if self.closed.is_set() or self.reserved_bytes+size>available_bytes or self.stage_bytes[stage]+size>byte_limit:
                    self.counts['skipped_budget']+=1;return False
                if full():
                    self.counts['skipped_limit_or_closed']+=1;return False
                try:self.queue.put_nowait((record,payloads,held_aperture,size))
                except queue.Full:self.counts['skipped_busy']+=1;return False
                self.counts['accepted']+=1;self.reasons[key]+=1;self.reserved_bytes+=size
                self.stages[stage]+=1;self.stage_bytes[stage]+=size
            return True
        except Exception:
            with self.lock:self.counts['capture_errors']+=1
            return False

    def status(self):
        with self.lock:
            return dict(self.counts,reserved_bytes=self.reserved_bytes,
                        pending=self.queue.unfinished_tasks,closed=self.closed.is_set())

    def _run(self):
        while not self.closed.is_set() or not self.queue.empty():
            try:record,payloads,held_aperture,size=self.queue.get(timeout=.05)
            except queue.Empty:continue
            try:
                persisted=[];failed=[]
                if held_aperture and any(hashlib.sha256(value).hexdigest()!=sha for sha,value in payloads.items()):
                    # No archive write has started; both the byte and sample
                    # reservation can be reused for the next complete sample.
                    with self.lock:
                        self.reserved_bytes-=size
                        self.stage_bytes['held_aperture']-=size
                    raise ValueError('held_aperture_source_hash_mismatch')
                for sha,value in payloads.items():
                    try:
                        if not held_aperture and hashlib.sha256(value).hexdigest()!=sha:raise ValueError('diagnostic_blob_hash_mismatch')
                        if self.archive.put_blob(value)!=sha:raise ValueError('diagnostic_store_hash_mismatch')
                        persisted.append(sha)
                    except Exception:failed.append(sha)
                record.update(persisted_blobs=persisted,failed_writes=failed,
                              complete=not record['missing_at_capture'] and not failed)
                if held_aperture and failed:raise ValueError('held_aperture_sources_not_persisted')
                encoded=json.dumps(record,allow_nan=False,sort_keys=True).encode()
                if len(encoded)>256*1024:raise ValueError('diagnostic_metadata_too_large')
                self.root.mkdir(exist_ok=True)
                final=self.root/(record['snapshot_id']+'.json');partial=final.with_suffix('.partial')
                with partial.open('xb') as stream:stream.write(encoded)
                partial.rename(final)
                with self.lock:self.counts['saved']+=1
            except Exception:
                with self.lock:
                    self.counts['write_errors']+=1
                    if held_aperture:
                        self.stages['held_aperture']-=1
                        self.counts['held_aperture_write_failures']+=1
            finally:self.queue.task_done()

    def close(self, timeout=1):
        self.closed.set();self.thread.join(timeout=timeout)
        return dict(self.status(),writer_alive=self.thread.is_alive())

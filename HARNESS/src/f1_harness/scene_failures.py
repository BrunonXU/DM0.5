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

    def offer(self, reason, snapshot, blob):
        """Best-effort enqueue. No serialization or disk writes on this call."""
        try:
            if snapshot is None:return False
            context=snapshot.get('context',{})
            if (context.get('authorized') is not True
                    or not all(context.get(k) for k in ('owner_instance_id','session_id','task_id','task_hash'))):
                return False
            phase=context.get('phase') or 'observe'
            stage='early' if phase in ('observe','prepared','approach_grasp') else 'later'
            # Reserve half the existing bounded archive for grasp/transport/place.
            # Startup errors cannot consume every post-motion diagnostic slot.
            sample_limit=max(1,self.max_samples//2) if stage=='early' else self.max_samples
            byte_limit=self.max_bytes//2 if stage=='early' else self.max_bytes
            key=(context['owner_instance_id'],context['session_id'],context['task_hash'],phase,str(reason)[:256])
            with self.lock:
                if self.closed.is_set() or self.counts['accepted']>=self.max_samples or self.stages[stage]>=sample_limit or self.reasons[key]>=self.per_reason:
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
            size=sum(len(value) for value in payloads.values())
            # Reserve a bounded metadata allowance too. Unreferenced partial
            # writes count against the budget even after a writer error.
            size+=256*1024
            with self.lock:
                if self.closed.is_set() or self.reserved_bytes+size>self.max_bytes or self.stage_bytes[stage]+size>byte_limit:
                    self.counts['skipped_budget']+=1;return False
                if self.counts['accepted']>=self.max_samples or self.stages[stage]>=sample_limit or self.reasons[key]>=self.per_reason:
                    self.counts['skipped_limit_or_closed']+=1;return False
                try:self.queue.put_nowait((record,payloads))
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
            try:record,payloads=self.queue.get(timeout=.05)
            except queue.Empty:continue
            try:
                persisted=[];failed=[]
                for sha,value in payloads.items():
                    try:
                        if hashlib.sha256(value).hexdigest()!=sha:raise ValueError('diagnostic_blob_hash_mismatch')
                        if self.archive.put_blob(value)!=sha:raise ValueError('diagnostic_store_hash_mismatch')
                        persisted.append(sha)
                    except Exception:failed.append(sha)
                record.update(persisted_blobs=persisted,failed_writes=failed,
                              complete=not record['missing_at_capture'] and not failed)
                encoded=json.dumps(record,allow_nan=False,sort_keys=True).encode()
                if len(encoded)>256*1024:raise ValueError('diagnostic_metadata_too_large')
                self.root.mkdir(exist_ok=True)
                final=self.root/(record['snapshot_id']+'.json');partial=final.with_suffix('.partial')
                with partial.open('xb') as stream:stream.write(encoded)
                partial.rename(final)
                with self.lock:self.counts['saved']+=1
            except Exception:
                with self.lock:self.counts['write_errors']+=1
            finally:self.queue.task_done()

    def close(self, timeout=1):
        self.closed.set();self.thread.join(timeout=timeout)
        return dict(self.status(),writer_alive=self.thread.is_alive())

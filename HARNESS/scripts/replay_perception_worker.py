"""Read-only real RGB-D/SAM worker for the offline formal task fixture.

Source timestamps are retained. Playback advances on observation requests,
not wall time. No robot address, actuator or model credential is accepted.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import signal
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlparse

from f1_harness.contracts import Observation
from f1_harness.evidence import EvidenceStore
from f1_harness.head_kinematics import HeadKinematics
from f1_harness.ipc import UnixServer
from f1_harness.live_scene import LiveSceneObserver
from f1_harness.mask_service import ConfiguredMaskProvider
from f1_harness.scene_pipeline import CalibratedScenePipeline


class PerceptionEvidenceStore(EvidenceStore):
    """Share immutable image blobs, never mix source events with virtual owner time."""
    def __init__(self,root,event_root):
        super().__init__(root)
        self.events=EvidenceStore(event_root)

    def event(self,kind,**data):
        return self.events.event(kind,**data)


class RecordedPerception:
    def __init__(self, capture, native_feedback, mask_config, sam_url, output, store, *, runtime=None):
        if urlparse(sam_url).hostname!='127.0.0.1':raise ValueError('local_sam_only')
        self.output=Path(output);self.output.mkdir(parents=True,exist_ok=False)
        capture,native_feedback,mask_config=map(Path,(capture,native_feedback,mask_config))
        self.sources={str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (capture,native_feedback,mask_config,Path(__file__))}
        self.capture=json.loads(capture.read_text());self.native=json.loads(native_feedback.read_text())
        if self.native['capture_sha256']!=self.sources[str(capture.resolve())]:raise ValueError('native_capture_mismatch')
        for blob in (capture.parent/'blobs').iterdir():
            if store.put_blob(blob.read_bytes())!=blob.name:raise ValueError('recorded_blob_changed')
        self.raw=[Observation.model_validate(s['observation']) for s in self.capture['snapshots']]
        self.index=0;self.status={};self.rows=[];self.calls=[];self.current=self.raw[0]
        self.lock=threading.Lock();self.scene=None;self.last_error='mask_uncertain_requires_new_prompt'
        config=deepcopy(self.capture['configuration']['scene'])
        config['geometry']['independent_fixture_localization']=True
        root=Path(__file__).resolve().parents[1];infra=root.parent/'F1-4242'
        self.head=HeadKinematics(dict(config['head_kinematics'],
            urdf_path=str(infra/'web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf'),
            mapping_path=str(infra/'configs/model_mapping.json')))
        masks=json.loads(mask_config.read_text())
        for view in masks['views'].values():
            if view['backend']!='sam2_remote':raise ValueError('actual_sam_for_all_views_required')
            view.update(sam_url=sam_url,timeout_seconds=10.,require_explicit_target_prompt=True)
            view.pop('point_prompts',None)
        self.provider=ConfiguredMaskProvider(masks)
        self.mask_counts={};segment=self.provider.segment_frame
        def recorded_masks(rgb,*,view,source,boxes=None):
            import cv2
            import numpy as np
            result=segment(rgb,view=view,source=source,boxes=boxes)
            self.mask_counts[view]=self.mask_counts.get(view,0)+1
            if self.mask_counts[view]<=3:
                name=f'{view}-{self.mask_counts[view]:03d}'
                overlay=np.array(rgb,copy=True)
                for index,(identity,mask) in enumerate(result.items()):
                    contours,_=cv2.findContours(mask.astype(np.uint8),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
                    cv2.drawContours(overlay,contours,-1,(30,230,80),2)
                    cv2.putText(overlay,identity,(10,20+index*20),cv2.FONT_HERSHEY_SIMPLEX,.5,(255,60,30),1)
                cv2.imwrite(str(self.output/(name+'.png')),cv2.cvtColor(overlay,cv2.COLOR_RGB2BGR))
                np.savez_compressed(self.output/(name+'.npz'),**result)
            return result
        self.provider.segment_frame=recorded_masks
        self.store=store
        from f1_harness.scene_evidence import bind_store
        self.scene_evidence_store=bind_store(Path(runtime) if runtime is not None else self.output.parent,store)
        observer=LiveSceneObserver.__new__(LiveSceneObserver)
        observer.raw=SimpleNamespace(store=store,observe=lambda:self.current);observer.store=store
        observer.owner=SimpleNamespace(call=lambda method:self.status)
        observer.config=config;observer.lock=threading.Lock();observer.image_issuances={}
        observer.pending_target_hints={};observer.target_hint_generation=0
        observer.latest=None;observer.error=self.last_error
        observer.pipeline=CalibratedScenePipeline(config['geometry'],self.provider,store.blob)
        self.observer=observer

    def observe(self):
        # Do not burn a finite recording while the model is deciding its prompt.
        if not self.observer.target_hint_generation:raise ValueError(self.last_error)
        if self.index+1>=len(self.raw):raise ValueError('offline_recording_exhausted')
        self.index+=1;obs=self.raw[self.index];self.current=obs
        context=self.status['perception_context']
        self.observer._apply_target_hint(obs,context)
        feedback=self.native['rows'][obs.observation_id]
        target=min(f.received_ns for f in obs.frames if f.camera=='right')
        state=min(self.capture['joint_samples'],key=lambda r:abs(r['received_ns']-target))
        measured=obs.model_copy(update=dict(state16=tuple(feedback['measured16']),
            state_valid_mask=tuple([True]*14+[False,True]),state_received_ns=state['received_ns']))
        started=time.perf_counter();scene=None;error=None
        try:
            scene=self.observer.pipeline.process(measured,base_eef=feedback['base_eef'],context=context,
                head_camera_pose=self.head.pose(feedback['full22']))
            if scene.received_ns!=obs.received_ns or any(f not in obs.frames for f in scene.frames):
                raise ValueError('recorded_source_identity_changed')
        except ValueError as exc:error=str(exc)
        self.rows.append(dict(source_observation_id=obs.observation_id,error=error,
            elapsed_ms=(time.perf_counter()-started)*1000,
            mask_diagnostics=self.provider.diagnostic_snapshot(),
            scene=None if scene is None else scene.model_dump(mode='json')))
        self.observer.latest=scene;self.observer.error=error;self.scene=scene;self.last_error=error
        if scene is None:raise ValueError(error)
        from f1_harness.scene_evidence import archive_observation,observation_wire
        scene,_=archive_observation(scene,self.store,self.scene_evidence_store['store_id'])
        self.observer.latest=self.scene=scene
        return dict(scene=observation_wire(scene),full22=feedback['full22'],
            physical_motion=False,source_clock_unchanged=True)

    def dispatch(self,method,kwargs):
        with self.lock:
            self.calls.append(method)
            if method=='ready':return dict(ready=True,actuator=False)
            self.status=kwargs.pop('status')
            if method=='camera_observation':return self.observer.camera_observation()
            if method=='locate_target':return self.observer.locate_target(**kwargs)
            if method=='readiness':return self.observer.readiness()
            if method=='observe':return self.observe()
            raise ValueError('read_only_recorded_perception_method')

    def close(self):
        self.observer.pipeline.close();self.provider.close()
        report=dict(physical_motion=False,robot_connected=False,source_clock_unchanged=True,
            playback='request paced; not real-time camera performance',calls=self.calls,rows=self.rows,
            source_sha256=self.sources,
            source_unchanged=all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==sha for p,sha in self.sources.items()))
        (self.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('capture','native-feedback','mask-config','output','store','socket'):
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--sam-url',required=True);a=p.parse_args()
    store=PerceptionEvidenceStore(a.store,a.output.parent/'perception-evidence')
    worker=RecordedPerception(a.capture,a.native_feedback,a.mask_config,a.sam_url,a.output,store,runtime=a.socket.parent)
    stop=threading.Event()
    for s in (signal.SIGTERM,signal.SIGINT):signal.signal(s,lambda *_:stop.set())
    server=UnixServer(a.socket,worker.dispatch,maximum=512*1024,scene_evidence=True).start()
    try:stop.wait()
    finally:server.close();worker.close()


if __name__=='__main__':main()

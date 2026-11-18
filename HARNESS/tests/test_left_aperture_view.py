from types import SimpleNamespace
import time

import cv2
import numpy as np

from f1_harness.aperture_image import aperture_features
from f1_harness.contracts import Frame
from f1_harness.evidence import FrameCache
from f1_harness.gateway import Gateway
from f1_harness.live_scene import LiveSceneObserver
from f1_harness.observation import ObservationBuffer
from f1_harness.ros_observer import RosObserver


def recorded_frame(camera, stamp, number, sha, frame_size):
    return Frame(camera=camera, stream='rgb', frame_id=camera, sequence=number, source_ns=stamp,
                 received_ns=stamp, clock_domain='native', width=frame_size[0], height=frame_size[1],
                 encoding='jpeg', blob_sha256=sha, publisher_id=camera)


def test_optional_left_frame_does_not_block_mandatory_views_and_keeps_acquisition_time():
    now = time.monotonic_ns()
    observer = RosObserver.__new__(RosObserver)
    observer.buffer = ObservationBuffer(clock=lambda:now)
    observer.inspection_buffer = ObservationBuffer(clock=lambda:now)
    frame = recorded_frame('head',now-30_000_000,1,'0'*64,(64,64))
    left = recorded_frame('left',now-29_000_000,1,'0'*64,(64,64))
    observer.buffer.add(frame)
    assert [f.camera for f in observer.observe().frames] == ['head']
    observer.inspection_buffer.add(left)
    observer.inspection_buffer.add(recorded_frame('left',now-1_000_000,2,'0'*64,(64,64)))
    assert [(f.camera,f.sequence) for f in observer.observe().frames] == [('head',1),('left',1)]
    assert observer.inspection_frame().sequence == 2
    observer.inspection_buffer.faults.add('resolution_changed:left')
    result = observer.observe()
    assert not result.faults and [f.camera for f in result.frames] == ['head']


def test_gateway_returns_local_aperture_candidates_without_model_images_or_motion():
    image = np.full((240,320,3),150,np.uint8)
    cv2.rectangle(image,(110,90),(143,123),(0,0,0),cv2.FILLED)
    cv2.fillConvexPoly(image,np.array([[50,85],[83,85],[50,120]]),0)
    cv2.circle(image,(90,150),18,0,cv2.FILLED)
    assert all(len(rows)==1 for rows in aperture_features(image).values())
    store = FrameCache()
    encoded = cv2.imencode('.jpg',image)[1].tobytes()
    frame = recorded_frame('left',12345,42,store.put_blob(encoded),(320,240))
    observer = LiveSceneObserver.__new__(LiveSceneObserver)
    observer.raw = SimpleNamespace(inspection_frame=lambda:frame,store=store)
    gateway = Gateway(None,observer,None)
    result = gateway.dispatch('aperture_observation')
    assert result['available'] and result['motion_evidence'] is False
    assert result['source_blob_sha256']==frame.blob_sha256 and result['sequence']==42
    assert all(len(rows)==1 for rows in result['candidates'].values())
    assert not {'pose','image','depth'} & result.keys()
    observer.raw.inspection_frame=lambda:None
    assert gateway.dispatch('aperture_observation')['available'] is False

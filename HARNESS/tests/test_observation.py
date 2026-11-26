import numpy as np
import pytest
from f1_harness.contracts import Frame, Observation, SceneObject, Task
from f1_harness.observation import ObservationBuffer, require_geometry
from f1_harness.images import decode
from f1_harness.verification import verify


def frame(stream, **kw):
    return Frame(**(dict(camera='head',stream=stream,frame_id='optical',sequence=1,source_ns=100,
                        received_ns=100,clock_domain='camera',width=2,height=2,encoding='16UC1',
                        blob_sha256='a'*64,camera_info_sha256='b'*64,depth_scale_m=.001) | kw))


def test_receipt_pair_is_not_geometry():
    buffer=ObservationBuffer(clock=lambda:100)
    buffer.add(frame('rgb'));buffer.add(frame('depth'))
    obs=buffer.snapshot().model_copy(update=dict(calibration_id='cal',stationary=True,state_received_ns=100,
                                                state16=(0.,)*16,state_valid_mask=(True,)*16))
    assert obs.pairings[0].quality=='receipt_only'
    with pytest.raises(ValueError,match='unverified RGB-D'):
        require_geometry(obs,now_ns=100,max_age_ns=100,required_cameras=('head',))


def test_duplicate_stamp_and_stale_frame_rejected():
    buffer=ObservationBuffer(source_clock_verified=True,clock=lambda:1000)
    buffer.add(frame('rgb'));buffer.add(frame('depth'))
    obs=buffer.snapshot().model_copy(update=dict(calibration_id='cal',stationary=True,state_received_ns=1000,
                                                state16=(0.,)*16,state_valid_mask=(True,)*16))
    with pytest.raises(ValueError,match='stale frame'):
        require_geometry(obs,now_ns=1000,max_age_ns=100,required_cameras=('head',))
    buffer.add(frame('rgb',sequence=2,received_ns=100+300_000_000,blob_sha256='c'*64))
    assert buffer.snapshot().pairings[0].quality=='invalid'


def test_stride_and_depth_units():
    f=frame('depth',row_step_bytes=6,is_bigendian=True)
    pixels=np.array([[500,600,0],[700,800,0]],dtype='>u2').tobytes()
    assert decode(f,pixels).tolist()==[[500,600],[700,800]]
    with pytest.raises(ValueError,match='meter_scale'):
        decode(f.model_copy(update={'depth_scale_m':1.}),pixels)


@pytest.mark.parametrize('hole,expected',[(None,'pending_review'),('wrong','failure'),('hole','success')])
def test_placement_requires_matching_hole(hole,expected):
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='cal')
    obj=SceneObject(object_id='block',shape='square',held=False,inserted=True,stationary=True,
                    inserted_hole_id=hole,evidence_ids=('visual',))
    obs=Observation(observation_id='1',acquisition_instance='a',received_ns=1,clock_domain='host',objects=(obj,))
    assert verify('verify_placed',task,obs,'r').outcome==expected

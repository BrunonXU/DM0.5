import time
import pytest
from f1_harness.engine import TaskEngine
from f1_harness.evidence import EvidenceStore
from f1_harness.owner_bridge import OwnerBridge,JointProposal
from f1_harness.contracts import Observation,Frame,Pairing,Task,HarnessError


def test_shadow_candidate_has_no_authority_or_physical_completion(tmp_path):
    now=time.monotonic_ns()
    engine=TaskEngine(EvidenceStore(tmp_path),mode='shadow',clock=lambda:now)
    session=engine.open_session()['session_id']
    engine.prepare_task(session,Task(task_id='test',object_id='block',hole_id='hole',calibration_id='test').model_dump())
    frames=[];pairs=[]
    for camera in ['head','right']:
        pairs.append(Pairing(camera=camera,quality='source_time_bounded',rgb_sequence=1,depth_sequence=1,skew_ns=0))
        for stream in ['rgb','depth']:
            frames.append(Frame(camera=camera,stream=stream,frame_id=camera,sequence=1,source_ns=now,
                received_ns=now,clock_domain='test',width=20,height=20,encoding='rgb8' if stream=='rgb' else '16UC1',
                blob_sha256='0'*64,camera_info_sha256='1'*64,depth_scale_m=.001 if stream=='depth' else None))
    engine.update_observation(Observation(observation_id='fixture',acquisition_instance='test',received_ns=now,
        clock_domain='test',frames=tuple(frames),pairings=tuple(pairs),state16=(0.,)*16,state_valid_mask=(True,)*16,
        state_received_ns=now,calibration_id='test',stationary=True))
    def planner(intent,obs,full,held):
        return JointProposal(intent.request_id,intent.epoch,intent.scene_version,intent.calibration_id,full,(held,),.1,100,1.)
    bridge=OwnerBridge(engine,numerical_planner=planner,result_verifier=lambda *a:None,
                       feedback_age_ns=1000,reference_tolerance_deg=.5,clock=lambda:now)
    try:
        bridge.update_feedback(full22=(0.,)*22,measured16=(0.,)*16,stamps16=(now,)*16)
        report=bridge.shadow_probe(session)
        assert report['kind']=='numerical_candidate_only' and not report['physical_completion']
        assert not engine.authorized and engine.pending is None and engine.running is None and not engine.results
        engine.mode='hardware'
        with pytest.raises(HarnessError,match='shadow_owner_required'):bridge.shadow_probe(session)
    finally:bridge.close()

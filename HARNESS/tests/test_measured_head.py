import copy
import json
from pathlib import Path

import numpy as np
import pytest

from f1_harness.head_kinematics import HeadKinematics, checked_head_model
from f1_harness.boot_recheck import build_boot_receipt
from f1_harness.scene_pipeline import CalibratedScenePipeline
from f1_harness.live_scene import LiveSceneObserver
from f1_harness.revalidation import tolerances
from f1_harness.contracts import HarnessError, Observation

ROOT=Path(__file__).resolve().parents[2]

@pytest.fixture
def recorded():
    area=ROOT/'HARNESS/artifacts/verified-deployment-20261110'
    diagnosis=area/'body-reference-diagnosis.json'
    if not diagnosis.exists():pytest.skip('saved F1 read-only evidence not installed')
    report=json.loads(diagnosis.read_text())
    paths={k:Path(v['path']) for k,v in report['inputs'].items()}
    cal=json.loads(paths['coarse_calibration'].read_text())
    raw=json.loads(paths['boot_evidence'].read_text())
    spec=dict(schema='f1-measured-head-v1',urdf_path=str(paths['urdf']),mapping_path=str(paths['mapping']),
        **cal['executor_identity'],T_head_Link3_camera=cal['cameras']['head']['T_head_Link3_camera'])
    scene=dict(reference_tolerance_deg=.5,geometry=dict(measured_head_pose=True),head_kinematics=spec)
    return cal,raw,scene,paths,report


def test_real_changed_posture_matches_independent_pinocchio(recorded):
    cal,raw,scene,_,report=recorded
    model=checked_head_model(scene,cal)
    assert np.allclose(model.pose(raw['body_head_deg']),report['T_world_camera_current_nominal'],atol=1e-12)
    assert np.allclose(model.pose(cal['body_head_reference_deg']),cal['cameras']['head']['T_world_camera'],atol=1e-6)
    assert np.linalg.norm(model.pose(raw['body_head_deg'])[:3,3]-model.pose(cal['body_head_reference_deg'])[:3,3])>.02


def test_old_boot_rejection_becomes_compensated_warning(recorded):
    cal,raw,scene,paths,_=recorded
    kw=dict(calibration=cal,calibration_sha256=raw['calibration_sha256'],scene=scene,
        task_hash='a'*64,current_boot_id=raw['current_boot_id'],now_ns=raw['observed_monotonic_ns']+1)
    result=build_boot_receipt(raw,paths['boot_evidence'],**kw)
    assert result['checks']['reference_drift_warning']
    assert result['checks']['head_pose_mode']=='measured_kinematics'
    assert not result['motion_authorized'] and not result['precision_certified']
    kw['scene']=dict(reference_tolerance_deg=.5)
    with pytest.raises(ValueError,match='body_head_reference_changed'):build_boot_receipt(raw,paths['boot_evidence'],**kw)


@pytest.mark.parametrize('kind',['flag_only','missing_flag','model_hash','handeye','old_reference'])
def test_compensation_requires_bound_original_calibration(recorded,kind):
    cal,_,scene,_,_=recorded
    if kind=='flag_only':scene.pop('head_kinematics')
    elif kind=='missing_flag':scene['geometry'].clear()
    elif kind=='model_hash':scene['head_kinematics']['urdf_sha256']='0'*64
    elif kind=='handeye':scene['head_kinematics']['T_head_Link3_camera']=np.eye(4).tolist()
    else:cal['cameras']['head']['T_world_camera']=np.eye(4).tolist()
    with pytest.raises(ValueError):checked_head_model(scene,cal)


@pytest.mark.parametrize('raises',[False,True])
def test_frame_pose_override_is_used_and_never_leaks(raises):
    p=object.__new__(CalibratedScenePipeline)
    p.config=dict(measured_head_pose=True,camera='right',object_id='square-1',
        hole_camera=dict(camera='head',base_camera=np.eye(4).tolist()))
    original=copy.deepcopy(p.config);p.segment=object();seen=[];p.reject_measurement=lambda reason:None
    observation=Observation(observation_id='frame-1',acquisition_instance='recorded',received_ns=1,clock_domain='recorded')
    pose=np.eye(4);pose[0,3]=.02
    def process(*a,**kw):
        seen.append(copy.deepcopy(p.config))
        if raises:raise ValueError('rejected current RGB-D')
        return observation
    p._process=process
    if raises:
        with pytest.raises(ValueError,match='rejected current RGB-D'):
            p.process(observation,base_eef=np.eye(4),head_camera_pose=pose)
    else:assert p.process(observation,base_eef=np.eye(4),head_camera_pose=pose) is observation
    assert seen[0]['hole_camera']['base_camera']==pose.tolist()
    assert p.config==original
    with pytest.raises(ValueError,match='measured_head_camera_pose_required'):p.process(observation,base_eef=np.eye(4))


def test_grasp_relaxation_does_not_relax_insertion():
    fixture=dict(object_dimensions_m=[.03,.03,.06],planning_margin_m=.015,
        object_planning_margin_m=.001,effective_clearance_m=.0027,
        revalidation=dict(xy_tolerance_m=.002,yaw_tolerance_rad=.087,tilt_tolerance_rad=.087,
                          grasp_position_tolerance_m=.008))
    assert tolerances(fixture,'grasp')['position_m']==.008
    assert tolerances(fixture,'regrasp')['position_m']==.008
    assert tolerances(fixture,'insert')['position_m']==pytest.approx(.0017)
    fixture['revalidation']['grasp_position_tolerance_m']=.03
    with pytest.raises(HarnessError,match='grasp_tolerance_outside_capture_region'):tolerances(fixture,'grasp')

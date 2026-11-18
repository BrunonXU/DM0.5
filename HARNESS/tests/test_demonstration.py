from dataclasses import replace
import numpy as np
from scipy.spatial.transform import Rotation
from f1_harness.demonstration import retarget,validate_template
from f1_harness.skills import propose
from test_full_development_skills import scene,pose


def test_curve_is_reanchored_without_replaying_old_positions_or_joints():
    segment=dict(eef_poses=[pose(10,0,1).tolist(),pose(10.5,.02,1.01).tolist(),pose(11,0,1).tolist()],
                 times_s=[0,1,2],arm_deg=[[1]*7,[2]*7,[3]*7])
    goals,refs=retarget(segment,pose(0,0,.4),pose(.2,0,.4))
    assert np.allclose(goals[0][:3,3],[.1,.004,.402])
    assert np.allclose(goals[-1],pose(.2,0,.4)) and refs==[[2]*7,[3]*7]


def test_demo_grasp_keeps_close_before_lift_and_records_all_joint_preferences():
    value=scene();plain=propose('grasp',value)
    grasp=np.array(plain['stages'][0]['goals'][-1]);lift=np.array(plain['stages'][1]['goals'][-1])
    def segment(a,b):return dict(eef_poses=[a.tolist(),b.tolist()],times_s=[0,1],arm_deg=[[0]*7,[1]*7])
    template=dict(schema='f1-success-trajectory-v1',source_run='measured-run',source_sha256='a'*64,
        measurement_kind='measured_feedback',segments={'grasp':segment(value.base_eef,grasp),'lift':segment(grasp,lift)})
    validate_template(template)
    result=propose('grasp',replace(value,success_trajectory=template))
    first,second=result['stages']
    assert first['gripper']==value.grasp_opening and second['gripper'] is None
    assert first['posture_references_deg']==[[1]*7] and second['trajectory_source']=='measured-run'
    assert np.allclose(first['goals'][-1],grasp) and np.allclose(second['goals'][-1],lift)


def test_padding_advisory_keeps_actual_obstacle_overlap_blocking():
    from f1_harness.skills import _motion_clear
    from f1_harness.geometry import tool_sweep_clear
    value=replace(scene(),tool_padding_policy='advisory',obstacles=({'min':[.10,-1,-1],'max':[1,1,1]},),
                  tool_local_boxes=({'min':[-.01,-.01,-.01],'max':[.01,.01,.01]},),eef_tip_reference=pose())
    near=pose(.085,0,0)
    assert _motion_clear(value,near,near)
    assert not _motion_clear(replace(value,tool_padding_policy='required'),near,near)
    contact=pose(.095,0,0)
    assert not _motion_clear(value,contact,contact)


def test_lift_is_rebased_to_solved_contact_and_keeps_object_at_measured_location():
    from f1_harness.skills import rebase_support_lift
    nominal=pose(.1,0,.5);actual=pose(.1003,0,.5006);obj=pose(.1,0,.4)
    stage=dict(attachment_basis='predicted_for_collision_only',goals=[pose(.1,0,.58).tolist()],gripper=None)
    rebound=rebase_support_lift(stage,actual,nominal,obj)
    assert np.allclose(rebound['goals'][0],pose(.1003,0,.5806))
    assert np.allclose(actual@np.array(rebound['eef_object']),obj)
    assert np.allclose(stage['goals'][0],pose(.1,0,.58))

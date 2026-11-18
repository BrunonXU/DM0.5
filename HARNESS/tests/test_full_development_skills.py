from dataclasses import replace
import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from f1_harness.geometry import attached_object_sweep_clear, tool_sweep_clear
from f1_harness.frames import FrameChain
from f1_harness.scene_fixture import bucket_obstacles, table_halfspace, grasp_eef_offset
from f1_harness.skills import SkillGeometry, propose, stage_obstacles


def pose(x=0,y=0,z=0):
    value=np.eye(4);value[:3,3]=[x,y,z];return value


def scene():
    return SkillGeometry(base_eef=pose(0,0,.5),base_object=pose(.2,0,.2),base_hole=pose(.5,0,.3),
        eef_object=pose(0,0,-.1),object_grasp_eef=pose(0,0,.1),approach_clearance_m=.08,
        transfer_clearance_m=.08,insertion_depth_m=.012,attached_radius_m=.2,uncertainty_m=None,
        effective_clearance_m=None,obstacles=(),grasp_opening=100,release_opening=18,
        eef_tip_reference=pose(0,0,-.08),tool_local_boxes=({'min':[-.02,-.02,-.08],'max':[.02,.02,.02]},),
        admission_profile='development_coarse',planning_margin_m=.015,object_planning_margin_m=.001,
        object_local_box={'min':[-.015,-.015,0],'max':[.015,.015,.06]},object_held=True,target_object_id='block')


def test_full_skills_no_precision_claim_or_20mm_limit():
    value=scene()
    for skill in ('approach_grasp','grasp','transfer_above_hole','align','insert','release','retreat'):
        result=propose(skill,value)
        assert result['kind']=='motion' and not result.get('partial_approach')
    goal=np.array(propose('approach_grasp',value)['stages'][0]['goals'][0])
    assert np.linalg.norm(goal[:3,3]-value.base_eef[:3,3])>.02
    with pytest.raises(RuntimeError,match='error_budget'):
        propose('insert',replace(value,admission_profile='precision'))


def test_registered_staging_retains_object_goal_and_long_rotation_branch():
    value=scene()
    current=value.base_eef.copy()
    current[:3,:3]=Rotation.from_euler('x',90,degrees=True).as_matrix()
    value=replace(value,base_eef=current,transfer_staging_pose=pose(-.2,-.3,.6),
                  transfer_rotation_path='opposite_shortest')
    goals=[np.array(g) for g in propose('transfer_above_hole',value)['stages'][0]['goals']]
    rotations=[np.linalg.norm(Rotation.from_matrix(b[:3,:3]@a[:3,:3].T).as_rotvec())
               for a,b in zip([current]+goals,goals)]
    assert np.rad2deg(sum(rotations))==pytest.approx(270)
    assert max(rotations)<np.pi
    expected=value.base_hole.copy();expected[:3,3]+=expected[:3,2]*value.transfer_clearance_m
    assert np.allclose(goals[-1]@value.eef_object,expected)
    assert np.allclose(goals[1][:3,3],[-.2,-.3,.6])
    # Staging is not exempt from the same tool/attached-object obstacle gates.
    obstacle=dict(min=[-.23,-.33,.55],max=[-.17,-.27,.65])
    with pytest.raises(RuntimeError,match='no_clear_square_symmetry_route'):
        propose('transfer_above_hole',replace(value,obstacles=(obstacle,)))


def test_staging_pose_uses_registered_frame_chain(monkeypatch):
    from dataclasses import asdict
    from f1_harness.contracts import Task, Observation, SceneObject
    from f1_harness.skill_services import PlanService
    value=scene();task=Task(task_id='t',object_id='block',hole_id='hole',calibration_id='cal',
                            admission_profile='development_coarse')
    service=object.__new__(PlanService);service.task=task
    world=pose(.4,.2,.1);world[:3,:3]=Rotation.from_euler('y',20,degrees=True).as_matrix()
    service.frames=FrameChain(world,pose(),'native','world')
    staging=pose(.55,-.42,.92)
    service.fixture=asdict(value)|dict(native_frame='native',transfer_staging_pose=staging.tolist(),
                                     transfer_rotation_path='opposite_shortest')
    obs=Observation(observation_id='o',acquisition_instance='a',received_ns=100,clock_domain='clock',
        objects=(SceneObject(object_id='block',shape='square',pose=tuple(value.base_object.ravel()),frame_id='native',held=True),
                 SceneObject(object_id='hole',shape='square',pose=tuple(value.base_hole.ravel()),frame_id='native')))
    result=service._scene(obs,[0.]*22,[0.]*16,base_eef=pose())
    assert np.allclose(result.transfer_staging_pose,world@staging)
    assert result.transfer_rotation_path=='opposite_shortest'


def test_insert_bottom_and_actual_held_offset_no_double_height():
    value=scene();result=propose('insert',value)
    eef=np.array(result['stages'][0]['goals'][0]);bottom=eef@value.eef_object
    assert np.allclose(bottom[:3,3],[.5,0,.288])
    assert np.isclose(eef[2,3],.388)
    for changed in (dict(object_held=None),dict(eef_object=None),dict(object_held=False)):
        with pytest.raises(RuntimeError,match='in_hand'):propose('insert',replace(value,**changed))


def test_target_only_contact_and_regrasp_reopens_first():
    target=dict(role='target_object',object_id='block',min=[.18,-.02,.18],max=[.22,.02,.28])
    neighbour=target|dict(object_id='neighbour')
    value=replace(scene(),object_held=False,obstacles=(target,neighbour))
    assert stage_obstacles(value)==(target,neighbour)
    assert stage_obstacles(value,allow_target_contact=True)==(neighbour,)
    assert stage_obstacles(replace(value,object_held=True))==(neighbour,)
    result=propose('regrasp',replace(value,obstacles=(target,)))
    assert result['stages'][0]['goals']==[] and result['stages'][0]['gripper']==18
    assert result['stages'][2]['allow_target_contact']
    assert result['stages'][-1]['support_contact_tolerance_m']==.001
    with pytest.raises(RuntimeError,match='confirmed_empty'):propose('regrasp',scene())


def test_offset_object_collision_and_rotating_corner_cannot_be_missed():
    block={'min':[-.01,-.01,-.01],'max':[.01,.01,.01]}
    obstacle={'min':[.19,-.02,-.02],'max':[.21,.02,.02]}
    assert not attached_object_sweep_clear(pose(),pose(),pose(.2),block,[obstacle],margin_m=.001)
    end=pose();end[:3,:3]=Rotation.from_euler('z',90,degrees=True).as_matrix()
    rotating={'min':[.99,-.01,-.01],'max':[1.01,.01,.01]}
    middle={'min':[.69,.69,-.02],'max':[.73,.73,.02]}
    assert not tool_sweep_clear(pose(),end,pose(),.01,[middle],margin_m=.001,local_boxes=[rotating])


def test_registered_tool_boxes_do_not_use_eef_centred_sphere():
    value=scene();obstacle={'min':[-.1,-.1,.65],'max':[.1,.1,.7]}
    assert tool_sweep_clear(value.base_eef,value.base_eef,value.eef_tip_reference,.2,[obstacle],
                            margin_m=.015,local_boxes=value.tool_local_boxes)
    assert not tool_sweep_clear(value.base_eef,value.base_eef,value.eef_tip_reference,.2,[obstacle],margin_m=.015)


def test_tilted_table_uses_plane_not_global_max_and_lift_contact():
    table=table_halfspace([-.029628,-.03455,.998963],[1.013256,-.110464,.556727])
    obj=pose(.8258,-.274,.54655);box={'min':[-.015,-.015,0],'max':[.015,.015,.06]}
    # 60mm block fits the local sloped support even though a global maximum .5797 would reject it.
    assert attached_object_sweep_clear(obj,pose(.8258,-.274,.65),pose(),box,[table],margin_m=0)
    assert not attached_object_sweep_clear(pose(.8258,-.274,.53),obj,pose(),box,[table],margin_m=0)
    horizontal=table_halfspace([0,0,1],[0,0,0])
    assert attached_object_sweep_clear(pose(),pose(0,0,.1),pose(),box,[horizontal],margin_m=0)


def test_oriented_bucket_channel_passes_object_but_not_tool():
    hole=pose(0,0,.3);hole[:3,:3]=Rotation.from_euler('z',30,degrees=True).as_matrix()
    obstacles=bucket_obstacles({'min':[-.15,-.15,0],'max':[.15,.15,.32]},hole,[.036,.036],
                               wall_thickness_m=.005,lid_thickness_m=.005)
    obj_start=hole@pose(0,0,.04);obj_end=hole@pose(0,0,-.012)
    box={'min':[-.015,-.015,0],'max':[.015,.015,.06]}
    object_obstacles=[o for o in obstacles if o['collision_scope']!='tool_only']
    assert attached_object_sweep_clear(obj_start,obj_end,pose(),box,object_obstacles,margin_m=.001)
    assert not attached_object_sweep_clear(obj_start,obj_end,pose(),box,obstacles,margin_m=.001)
    offset=obj_end.copy();offset[:3,3]+=hole[:3,0]*.01
    assert not attached_object_sweep_clear(obj_start,offset,pose(),box,object_obstacles,margin_m=.001)


def test_frame_chain_preserves_obstacle_roles_and_plane_geometry():
    world=pose(.3,0,.2);chain=FrameChain(world,pose(),'native','world')
    plane=table_halfspace([0,0,1],[0,0,0]);box=dict(min=[0,0,0],max=[1,1,1],pose=pose().tolist(),
                                               role='target_object',object_id='block')
    transformed=chain.world_obstacles([plane,box])
    assert transformed[0]['point']==[.3,0,.2] and transformed[0]['role']=='table'
    assert transformed[1]['object_id']=='block' and transformed[1]['pose']==world.tolist()


def test_plan_service_binds_coarse_profile_and_passes_only_numerical_stage_arguments(monkeypatch):
    from dataclasses import asdict
    from f1_harness.contracts import Task,SkillIntent,Observation,SceneObject,Frame,Pairing,HarnessError,digest
    from f1_harness.skill_services import PlanService
    import f1_harness.revalidation as revalidation
    value=replace(scene(),object_held=False)
    task=Task(task_id='t',object_id='block',hole_id='hole',calibration_id='cal',admission_profile='development_coarse')
    service=object.__new__(PlanService);service.task=task;service.clock=lambda:100
    from f1_harness.candidate_failures import CandidateFailures
    service.candidate_failures=CandidateFailures()
    service.frames=FrameChain(pose(),pose(),'native','world')
    service.fixture=asdict(value)|dict(native_frame='native',joint_tolerance_deg=.1,settle_ms=20,gripper_tolerance=1)
    calls=[];guards=[]
    class Numerics:
        def call(self,method,**kwargs):
            calls.append((method,kwargs))
            if method=='forward_pose':return value.base_eef.tolist()
            assert set(kwargs)=={'goals','gripper','full22','held16','scene'}
            assert 'maximum_approach_translation_m' not in kwargs['scene']
            assert kwargs['scene']['uncertainty_m']==.015
            return [[0.]*16]
    service.numerics=Numerics()
    original_guard=revalidation.require_planning_geometry
    def guard(obs,**kw):
        guards.append(kw)
        return original_guard(obs,**kw)
    monkeypatch.setattr(revalidation,'require_planning_geometry',guard)
    frames=tuple(Frame(camera=camera,stream=stream,frame_id=camera+'_'+stream,sequence=1,
        source_ns=100,received_ns=100,clock_domain='clock',width=2,height=2,
        encoding='rgb8' if stream=='rgb' else '16UC1',blob_sha256='0'*64,
        camera_info_sha256='1'*64,depth_scale_m=.001 if stream=='depth' else None)
        for camera in ('head','right') for stream in ('rgb','depth'))
    obs=Observation(observation_id='o',acquisition_instance='a',received_ns=100,clock_domain='clock',
        calibration_id='cal',scene_version='v',stationary=True,geometry_scope='coarse_static_task',
        geometry_camera='right',frames=frames,state16=(0.,)*16,state_valid_mask=(True,)*16,state_received_ns=100,
        pairings=tuple(Pairing(camera=c,quality='receipt_only',rgb_sequence=1,depth_sequence=1,skew_ns=0)
                       for c in ('head','right')),
        objects=(SceneObject(object_id='block',shape='square',pose=tuple(value.base_object.ravel()),frame_id='native',held=False),
                 SceneObject(object_id='hole',shape='square',pose=tuple(value.base_hole.ravel()),frame_id='native')))
    intent=SkillIntent(request_id='r',session_id='s',owner_instance_id='owner',task_id='t',task_hash=digest(task),
        epoch=0,observation_id='o',scene_version='v',calibration_id='cal',skill='grasp')
    service.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),held16=[0.]*16,full22=[0.]*22))
    assert guards[0]['task'].admission_profile=='development_coarse'
    assert guards[0]['intent']==intent and guards[0]['max_age_ns']==500_000_000
    stages=[args for method,args in calls if method=='solve_path']
    assert len(stages)==2 and stages[1]['scene']['object_planning_margin_m']==.001
    assert stages[1]['scene']['support_contact_tolerance_m']==.001
    assert stages[1]['scene']['eef_object'] is not None
    count=len(calls)
    wrong=intent.model_copy(update={'calibration_id':'other-calibration'})
    with pytest.raises(HarnessError,match='plan_binding_mismatch'):
        service.dispatch('plan_skill',dict(intent=wrong.model_dump(),observation=obs.model_dump(),
                                         held16=[0.]*16,full22=[0.]*22))
    assert len(calls)==count


def test_support_contact_is_bounded_and_must_separate_without_changing_other_margins():
    box={'min':[-.015,-.015,0],'max':[.015,.015,.06]}
    table=table_halfspace([0,0,1],[0,0,0])
    assert attached_object_sweep_clear(pose(0,0,-.0007),pose(0,0,.08),pose(),box,[table],
                                       margin_m=.001,support_contact_tolerance_m=.001)
    for a,b in ((-.002,.08),(-.0007,-.0008)):
        assert not attached_object_sweep_clear(pose(0,0,a),pose(0,0,b),pose(),box,[table],
                                               margin_m=.001,support_contact_tolerance_m=.001)
    assert not attached_object_sweep_clear(pose(0,0,-.0007),pose(0,0,.08),pose(),box,[table],margin_m=.001)
    neighbour=dict(min=[.0155,-.01,0],max=[.03,.01,.1])
    assert not attached_object_sweep_clear(pose(0,0,-.0007),pose(0,0,.08),pose(),box,[table,neighbour],
                                           margin_m=.001,support_contact_tolerance_m=.001)


def test_support_departure_allows_small_upward_rotational_tracking_error():
    box={'min':[-.015,-.015,0],'max':[.015,.015,.06]}
    table=table_halfspace([0,0,1],[0,0,0])
    start=pose(0,0,-.0007);end=pose(0,0,.0033)
    end[:3,:3]=Rotation.from_euler('y',.01,degrees=True).as_matrix()
    assert attached_object_sweep_clear(start,end,pose(),box,[table],margin_m=.001,
                                       support_contact_tolerance_m=.001)
    end[2,3]=start[2,3]
    assert not attached_object_sweep_clear(start,end,pose(),box,[table],margin_m=.001,
                                           support_contact_tolerance_m=.001)


def test_top_down_grasp_uses_measured_normal_and_closest_square_axis():
    obj=pose(.2,.1,.3);obj[:3,:3]=Rotation.from_euler('zy', [20,4],degrees=True).as_matrix()
    current=pose();tip=pose(0,-.249,.009)
    offset=np.array(grasp_eef_offset(obj,current,tip,height_above_bottom_m=.05,approach='object_normal'))
    eef=obj@offset
    assert np.allclose(eef[:3,1],obj[:3,2])
    assert np.allclose((eef@tip)[:3,3],(obj@pose(0,0,.05))[:3,3])


def test_square_yaw_selection_checks_future_insert_tool_clearance():
    tool=({'min':[.09,-.01,-.08],'max':[.11,.01,-.06]},)
    obstacle={'min':[.58,-.02,.30],'max':[.62,.02,.32]}
    value=replace(scene(),tool_local_boxes=tool,obstacles=(obstacle,))
    aligned=np.array(propose('align',value)['stages'][0]['goals'][-1])
    assert not np.allclose(aligned[:3,:3],np.eye(3))
    assert propose('insert',replace(value,base_eef=aligned))['kind']=='motion'


def test_horizontal_pregrasp_keeps_registered_height_and_checks_sweeps(monkeypatch):
    from dataclasses import replace
    import f1_harness.skills as module
    original=scene()
    current=original.base_eef.copy();current[0,3]-=.15
    geometry=replace(original,base_eef=current,approach_direction='from_current_horizontal')
    checks=[]
    monkeypatch.setattr(module,'_motion_clear',lambda s,a,b,*args,**kw:checks.append((a,b)) or True)
    goal=np.asarray(module.propose('approach_grasp',geometry)['stages'][0]['goals'][-1])
    grasp=geometry.base_object@geometry.object_grasp_eef
    displacement=goal[:3,3]-grasp[:3,3]
    assert np.linalg.norm(displacement)==pytest.approx(geometry.approach_clearance_m)
    assert np.dot(displacement,geometry.base_object[:3,2])==pytest.approx(0.)
    assert checks
    monkeypatch.setattr(module,'_motion_clear',lambda *a,**kw:False)
    with pytest.raises(module.HarnessError):module.propose('approach_grasp',geometry)


def test_registered_pregrasp_uses_clear_direct_distance_and_preserves_grasp(monkeypatch):
    import f1_harness.skills as module
    value=replace(scene(),object_held=False,pregrasp_distances_m=(.08,.06))
    # A high obstacle blocks the farther vertical pregrasp, not the final grasp.
    monkeypatch.setattr(module,'_motion_clear',lambda s,a,b,*args,**kw:b[2,3]<.37)
    stage=module.propose('approach_grasp',value)['stages'][0]
    assert stage['pregrasp_distance_m']==.06 and len(stage['goals'])==1
    np.testing.assert_allclose(np.array(stage['goals'][0])[:3,3],[.2,0,.36])
    monkeypatch.setattr(module,'_motion_clear',lambda *args,**kw:True)
    # Once chosen, revalidation cannot silently switch to a now-clear 8 cm goal.
    locked=module.propose('approach_grasp',value,pregrasp_distance_m=.06)['stages'][0]
    np.testing.assert_allclose(locked['goals'],stage['goals'])
    grasp=module.propose('grasp',value)['stages']
    reference=module.propose('grasp',replace(value,pregrasp_distances_m=None))['stages']
    assert grasp==reference
    with pytest.raises(module.HarnessError,match='unregistered_pregrasp_distance'):
        module.propose('approach_grasp',value,pregrasp_distance_m=.04)
    with pytest.raises(module.HarnessError,match='scope_mismatch'):
        module.propose('grasp',value,pregrasp_distance_m=.06)


@pytest.mark.parametrize('distances',[(.09,),(.08,0),(.08,float('nan')),(.08,True),(.08,.08)])
def test_pregrasp_registration_cannot_extend_range_or_use_invalid_values(distances):
    with pytest.raises(RuntimeError,match='registered_pregrasp_distances'):
        propose('approach_grasp',replace(scene(),pregrasp_distances_m=distances))


def test_success_pitch_changes_orientation_but_not_registered_grasp_point():
    from dataclasses import asdict
    from f1_harness.contracts import Task,Observation,SceneObject
    from f1_harness.skill_services import PlanService
    value=scene();service=object.__new__(PlanService)
    service.task=Task(task_id='t',object_id='block',hole_id='hole',calibration_id='cal',admission_profile='development_coarse')
    service.frames=FrameChain(np.eye(4),np.eye(4),'native','world')
    service.fixture=asdict(value)|dict(native_frame='native',grasp_pitch_source='success_posture_prior')
    reference=np.eye(4);reference[:3,:3]=Rotation.from_euler('x',20,degrees=True).as_matrix()
    service.posture_prior={'eef_poses':{'grasp':reference.tolist()}}
    obs=Observation(observation_id='o',acquisition_instance='a',received_ns=100,clock_domain='clock',objects=(
        SceneObject(object_id='block',shape='square',pose=tuple(value.base_object.ravel()),frame_id='native'),
        SceneObject(object_id='hole',shape='square',pose=tuple(value.base_hole.ravel()),frame_id='native')))
    result=service._scene(obs,[0.]*22,[0.]*16,base_eef=np.eye(4))
    np.testing.assert_allclose((result.object_grasp_eef@value.eef_tip_reference)[:3,3],
                               (value.object_grasp_eef@value.eef_tip_reference)[:3,3])
    np.testing.assert_allclose(result.object_grasp_eef[:3,:3],reference[:3,:3],atol=1e-12)


def test_rotated_tool_aabb_overlap_uses_separating_axis_without_reducing_margin():
    angle=np.pi/4;tool=pose();tool[:2,:2]=[[np.cos(angle),-np.sin(angle)],[np.sin(angle),np.cos(angle)]]
    box={'min':[-1,-.05,-.05],'max':[1,.05,.05]}
    # Disjoint tool and obstacle have overlapping axis-aligned boxes.
    clear={'min':[.60,-.70,-.02],'max':[.70,-.60,.02]}
    hit={'min':[.60,.60,-.02],'max':[.70,.70,.02]}
    assert tool_sweep_clear(tool,tool,pose(),.01,[clear],margin_m=.015,local_boxes=[box])
    assert not tool_sweep_clear(tool,tool,pose(),.01,[hit],margin_m=.015,local_boxes=[box])
    # Physical separation can still be smaller than the preserved padding.
    near={'min':[.10,-.01,-.02],'max':[.11,0,.02]}
    assert tool_sweep_clear(tool,tool,pose(),.01,[near],margin_m=0,local_boxes=[box])
    assert not tool_sweep_clear(tool,tool,pose(),.01,[near],margin_m=.03,local_boxes=[box])


def test_grasp_height_search_keeps_contact_in_object_and_original_table_margin():
    from f1_harness.skills import _clear_grasp_height
    table={'type':'halfspace','role':'table','normal':[0,0,1],'point':[0,0,.21]}
    value=replace(scene(),grasp_height_policy='table_clearance_within_object',obstacles=(table,))
    original=value.base_object@value.object_grasp_eef
    goal=_clear_grasp_height(value,original)
    assert np.allclose(goal[:3,:3],original[:3,:3])
    assert np.allclose(goal[:2,3],original[:2,3])
    assert goal[2,3]-original[2,3]==pytest.approx(.006)
    assert tool_sweep_clear(goal,goal,value.eef_tip_reference,.2,[table],margin_m=.015,local_boxes=value.tool_local_boxes)
    point=np.linalg.inv(value.base_object)@goal@value.eef_tip_reference
    assert .001<=point[2,3]<=.059
    blocked=replace(value,obstacles=({**table,'point':[0,0,.28]},))
    with pytest.raises(RuntimeError,match='grasp_height_outside_registered_object'):_clear_grasp_height(blocked,original)

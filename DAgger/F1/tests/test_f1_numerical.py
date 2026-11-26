from pathlib import Path
"""Real numerical stack; no ROS imports, robot sockets or action endpoints."""
import json
from types import SimpleNamespace
import numpy as np
import pytest
pin=pytest.importorskip('pinocchio')
pytest.importorskip('pink')
pytest.importorskip('ruckig')
from f1dagger.config import DAGGER_ROOT,load_config,infra_import
from f1dagger.contract import JointMapping,FULL_NAMES,MODEL_NAMES,home_target
from f1dagger.teleop import F1ArmTarget,HomeMotion,ArmBoundaryHold
from f1dagger.robot_io import CollisionGate,make_output

@pytest.fixture(scope='module')
def setup():
    root=Path(__import__('os').environ.get('F1_INFRA_ROOT', str(DAGGER_ROOT.parent.parent/'F1-4242')))
    c,p=load_config(DAGGER_ROOT/'configs/f1_09_hil.json',infra_root=root,profile_path=__import__('os').environ.get('F1_PROFILE_PATH',str(root/'configs/f1-09.json')))
    u=root/'web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf'
    m=JointMapping(json.loads((root/'configs/model_mapping.json').read_text()),p,u)
    full=np.array([p['default_presets'][1]['joints'][k] for k in FULL_NAMES],dtype=float)
    return c,p,m,u,full

@pytest.mark.parametrize('side',['left','right'])
def test_reduced_fk_matches_workbench_with_body_and_head_feedback(setup,side):
    c,p,m,u,full=setup
    geometry=infra_import(c['_infra_root'],'robot.geometry')
    for perturb in [np.zeros(22),np.linspace(-1,1,22)]:
        pose=full+perturb
        a=F1ArmTarget(u,m,c['ik']);a.begin(side,pose)
        frame='arm_L_Link7' if side=='left' else 'arm_R_Link7'
        reference=np.array(geometry.frames(dict(zip(FULL_NAMES,pose)))[frame])
        np.testing.assert_allclose(a.ik.takeover_pose.homogeneous,reference,atol=1e-9)

@pytest.mark.parametrize('side',['left','right'])
def test_pink_base_xyz_direction_limits_and_inactive_arm(setup,side):
    c,p,m,u,full=setup
    offset=0 if side=='left' else 7
    for axis in range(3):
        for sign in [-1,1]:
            a=F1ArmTarget(u,m,c['ik']);a.begin(side,full)
            start=a.ik.takeover_pose.translation.copy()
            state=np.r_[full[:14],43.,66.]
            axes=np.eye(3)[axis]*sign*.2
            packet=SimpleNamespace(active_side=side,mode='translation',axes=axes,angular_axes=(0,0,0),speed_scale=1.)
            for _ in range(10):
                before=state.copy();state=a.target(packet,full,state,.02)
                assert np.max(np.abs(np.deg2rad(state[:14]-before[:14])))<=c['ik']['safety']['max_joint_step_rad']+1e-9
            model22=m.to_model(np.r_[state[:14],full[14:]])
            cfg=a.ik.pink.Configuration(a.ik.model,a.ik.model.createData(),model22[offset:offset+7])
            displacement=cfg.get_transform_frame_to_world(a.ik.frame).translation-start
            assert sign*displacement[axis]>0
            other=slice(7,14) if side=='left' else slice(0,7)
            np.testing.assert_equal(state[other],full[other]);np.testing.assert_equal(state[14:],[43,66])


def test_actual_output_holds_and_home_is_incremental(setup):
    c,p,m,u,full=setup
    output=make_output(c)
    state=np.r_[full[:14],43.,66.]
    target=state.copy();target[0]-=10
    motion=HomeMotion(p,state,target)
    waypoint=motion.propose(.02)
    assert 0<state[0]-waypoint[0]<.1
    np.testing.assert_equal(waypoint[14:],state[14:]);motion.commit()
    output.push(state[:14],1.,enabled=(False,False))
    output.push(target[:14],1.02,enabled=(True,False))
    q=output.sample(1.024)
    assert 0<state[0]-q[0]<.1
    output.push(q,1.028,enabled=(False,False))
    np.testing.assert_allclose(output.sample(1.032),q,atol=1e-12)


def test_real_collision_and_com_use_identical_fk(setup,tmp_path):
    mujoco=pytest.importorskip('mujoco')
    c,p,m,u,full=setup
    guard=CollisionGate(c,m,tmp_path,full)
    assert guard.check(np.r_[full[:14],0,0],full)['safe']
    model=guard.protector._collision_guard._model
    data=mujoco.MjData(model)
    for name,value in zip(MODEL_NAMES,m.to_model(full)):
        jid=mujoco.mj_name2id(model,mujoco.mjtObj.mjOBJ_JOINT,name)
        data.qpos[model.jnt_qposadr[jid]]=value
    mujoco.mj_forward(model,data)
    for side in ['left','right']:
        a=F1ArmTarget(u,m,c['ik']);a.begin(side,full)
        bid=mujoco.mj_name2id(model,mujoco.mjtObj.mjOBJ_BODY,a.ik.frame)
        np.testing.assert_allclose(data.xpos[bid],a.ik.takeover_pose.translation,atol=2e-6)
    unsafe=np.array([149.021775,39.864277,148.495636,32.261501,-100.341137,43.935292,41.535349,-79.079957,76.567320,130.018930,-83.615231,9.626982,-51.421583,14.982914,0,0])
    with pytest.raises(RuntimeError,match='rejected'):guard.check(unsafe,full)


def test_warmup_discards_references_and_does_not_change_feedback(setup):
    c,p,m,u,full=setup
    a=F1ArmTarget(u,m,c['ik']);state=np.r_[full[:14],43.,66.]
    before_full,before_state=full.copy(),state.copy()
    timings=a.warmup(full,state)
    assert set(timings)=={'left_ms','right_ms'}
    assert a.ik is None and a.side is None
    np.testing.assert_array_equal(full,before_full);np.testing.assert_array_equal(state,before_state)


# Actual accepted commands and inputs immediately before the first four
# 2026-10-08 J2 rejections. Case 2 uses its last nonzero input: the real rejection
# happened after the stick centered while the old target still had a lead.
BOUNDARY_CASES = [('right', [-62.38954686709319, -93.95160963358985, 69.30942195369654, -81.17520864537391, -9.915985717811717, 18.111491872170323, 2.8351749988846446], [-63.369022369384766, -93.36997985839844, 70.08711242675781, -82.02217102050781, -10.619206428527832, 18.442899703979492, 3.279872179031372], [0.803924560546875, 0.5989366011186079, 0.0], [0.0, 0.0, -0.0]), ('left', [69.78907448160558, -93.98014958869959, -85.05580940134261, -85.27778183135814, 2.607242136047247, -0.26133131417272587, 4.185029047602564], [69.92662811279297, -93.89552307128906, -85.96620178222656, -85.59722137451172, 2.623095750808716, -0.207408145070076, 3.4205727577209473], [0.0017977627840909142, -0.08197576349431819, 0.0], [0.0, 0.0, -0.0]), ('right', [-60.55087964011418, -93.96673082896079, 69.93138408612833, -78.87618928601115, -9.193763429002672, 20.73266447037165, 3.392284083569664], [-60.56654357910156, -93.55601501464844, 69.96439361572266, -78.98109436035156, -9.685808181762695, 19.661222457885742, 3.003765106201172], [-0.0, -0.0, 0.0], [0.0, 0.9999653209339489, -0.0]), ('right', [-58.957175440633904, -93.95821646440797, 68.41436451923389, -76.88039272842221, -8.168443105034125, 20.603099980965997, 1.4229866486608795], [-59.944801330566406, -93.54002380371094, 69.48395538330078, -77.82313537597656, -9.101960182189941, 20.6982421875, 2.1408851146698], [0.4474237615411932, 0.9643499200994318, 0.0], [0.0, 0.0, -0.0])]


def _intent(side, axes=(0., 0., 0.), angular=(0., 0., 0.)):
    return SimpleNamespace(active_side=side, mode='combined', axes=axes,
                           angular_axes=angular, speed_scale=1.)


@pytest.mark.parametrize('side,command_arm,measured_arm,axes,angular', BOUNDARY_CASES)
def test_recorded_joint_boundary_holds_and_reverse_releases_without_reclutch(
        setup, side, command_arm, measured_arm, axes, angular):
    c,p,m,u,full=setup
    full=full.copy();offset=0 if side=='left' else 7
    full[offset:offset+7]=measured_arm
    original_feedback=full.copy()
    command=np.r_[full[:14],43.,66.]
    command[offset:offset+7]=command_arm
    a=F1ArmTarget(u,m,c['ik'])
    # Continuous RB: no release, centered interval, START or re-selection.
    minimum_margin = float('inf')
    for _ in range(500):
        command=a.target(_intent(side,axes,angular),full,command,.02)
        m.check_action(command)
        minimum_margin=min(minimum_margin, command[offset+1]-m.limits[offset+1,0])
        expected=a.ik.configuration.get_transform_frame_to_world(a.ik.frame)
        np.testing.assert_allclose(a.ik.target_pose.homogeneous,expected.homogeneous,atol=1e-12)
    assert minimum_margin < .05
    stopped=command.copy()
    for _ in range(25):
        command=a.target(_intent(side,[-v for v in axes],[-v for v in angular]),full,command,.02)
        m.check_action(command)
    assert command[offset+1]>stopped[offset+1]+.01
    other=slice(7,14) if side=='left' else slice(0,7)
    np.testing.assert_array_equal(command[other],stopped[other])
    np.testing.assert_array_equal(command[14:],stopped[14:])
    np.testing.assert_array_equal(full,original_feedback)


def test_command_seed_preserves_outside_feedback_and_zero_input_continuity(setup):
    c,p,m,u,full=setup
    full=full.copy()
    low=float(m.limits[1,0])
    command=np.r_[full[:14],43.,66.]
    command[1]=low+0.01985041130041
    full[1]=low-0.09674072265625
    original=full.copy()
    a=F1ArmTarget(u,m,c['ik'])
    for _ in range(12):
        result=a.target(_intent('left'),full,command,.02)
        np.testing.assert_allclose(result,command,atol=1e-10)
        m.check_action(result)
    assert a.seed_diagnostics['source']=='last_submitted'
    assert a.seed_diagnostics['measured_arm_deg'][1]<low
    assert a.seed_diagnostics['seed_arm_deg'][1]>low
    assert a.seed_diagnostics['max_feedback_error_deg']==pytest.approx(.11659113395666)
    np.testing.assert_array_equal(full,original)
    a.release()
    assert a.seed_diagnostics is None


def test_invalid_last_publication_is_not_repaired_or_converted_to_boundary(setup):
    c,p,m,u,full=setup
    low_left=float(m.limits[1,0])
    command=np.r_[full[:14],43.,66.];command[1]=low_left-0.001
    a=F1ArmTarget(u,m,c['ik'])
    with pytest.raises(ValueError,match='outside F1 HAL limits'):
        a.target(_intent('left'),full,command,.02)
    assert a.ik is None
    command_r=np.r_[full[:14],43.,66.];command_r[8]=-94.001
    with pytest.raises(ValueError,match='outside F1 HAL limits'):
        a.target(_intent('right'),full,command_r,.02)
    assert a.ik is None


@pytest.mark.parametrize('wrapped',[False,True])
def test_only_typed_pink_joint_limit_is_manual_boundary(setup,monkeypatch,wrapped):
    from pink.exceptions import NotWithinConfigurationLimits
    from g2dagger.pink_ik import IKHold
    c,p,m,u,full=setup
    command=np.r_[full[:14],43.,66.]
    a=F1ArmTarget(u,m,c['ik']);a.target(_intent('left'),full,command,.02)
    def failed(*args):
        error=NotWithinConfigurationLimits(1,-1.642297941,-1.640609496,2.094395102)
        if not wrapped:
            raise error
        try:
            raise error
        except NotWithinConfigurationLimits as cause:
            raise IKHold('wrapped limit error') from cause
    monkeypatch.setattr(a.ik,'solve',failed)
    with pytest.raises(ArmBoundaryHold) as caught:
        a.target(_intent('left',(1.,0.,0.)),full,command,.02)
    assert caught.value.details['reason']=='pink_joint_limit'
    np.testing.assert_allclose(a.ik_virtual_q,np.deg2rad(command[:7]*m.signs[:7]),atol=0)
    def unrelated(*args):
        raise IKHold('Pink solve failed: NotWithinConfigurationLimits text is not a typed boundary')
    monkeypatch.setattr(a.ik,'solve',unrelated)
    with pytest.raises(IKHold):
        a.target(_intent('left'),full,command,.02)


@pytest.mark.parametrize('side',['left','right'])
def test_coupled_wrist_boundary_discards_candidate_and_target_lead(setup,monkeypatch,side):
    from dataclasses import replace
    c,p,m,u,full=setup
    full=full.copy();offset=0 if side=='left' else 7
    command=np.r_[full[:14],43.,66.]
    command[offset+5:offset+7]=[50.,59.2]  # 110.45 < 110.5; individual bounds valid.
    a=F1ArmTarget(u,m,c['ik']);a.target(_intent(side),full,command,.02)
    candidate=a.last_candidate
    # Pink only has individual model limits; this legal-size step crosses the
    # HAL coupled wrist boundary without crossing either individual limit.
    q=candidate.q.copy();q[6]+=np.deg2rad(.1)*m.signs[offset+6]
    monkeypatch.setattr(a.ik,'solve',lambda *_:replace(candidate,q=q))
    with pytest.raises(ArmBoundaryHold) as caught:
        a.target(_intent(side,(1.,0.,0.)),full,command,.02)
    assert caught.value.details['reason']=='arm_wrist_limit'
    m.check_action(command)
    np.testing.assert_allclose(a.ik_virtual_q,np.deg2rad(command[offset:offset+7]*m.signs[offset:offset+7]),atol=0)
    np.testing.assert_array_equal(a.filtered_linear,np.zeros(3))

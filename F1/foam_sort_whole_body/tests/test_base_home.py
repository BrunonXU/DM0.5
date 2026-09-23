import math
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'robot'))
from f1_replay.base_home import error_to_target, velocity, validate_anchor, POLICY, HomeBackend
from f1_replay.runtime import ReplayError


def test_world_error_rotates_into_body_at_ninety_degrees():
    assert error_to_target((0,0,math.pi/2), (1,0,math.pi/2)) == pytest.approx((0,-1,0))


def test_heading_wrap_does_not_command_a_full_rotation():
    result=error_to_target((0,0,math.pi-.01), (0,0,-math.pi+.01))
    assert result[2] == pytest.approx(.02)


def test_acceleration_and_diagonal_speed_are_vector_limited():
    previous=(0,0,0)
    for _ in range(100):
        result=velocity((0,0,0),(1,1,0),previous,.05)
        assert math.hypot(*result[:2]) <= .1+1e-10
        assert math.hypot(result[0]-previous[0],result[1]-previous[1]) <= .08*.05+1e-10
        assert result[2] == 0
        previous=result


def test_tolerance_returns_zero_even_if_prior_command_nonzero():
    assert velocity((0,0,0),(.01,.01,0),(.1,0,0),.05)==(0,0,0)


@pytest.mark.parametrize('target',[(4,0,0),(0,0,.1)])
def test_rejects_long_or_rotating_return(target):
    with pytest.raises(ReplayError):velocity((0,0,0),target,(0,0,0),.05)


def anchor():
    return dict(schema='f1_base_home_odometry_v1',boot_id='boot',host='ms-thor',created_unix_s=100,xy_yaw=[1,2,0])


@pytest.mark.parametrize('key,value',[('boot_id','new'),('host','other'),('created_unix_s',-4000),('xy_yaw',[float('nan'),0,0])])
def test_rejects_stale_wrong_robot_or_invalid_anchor(key,value):
    a=anchor();a[key]=value
    with pytest.raises((ReplayError,ValueError)):validate_anchor(a,'boot','ms-thor',101)


def test_valid_anchor():
    assert validate_anchor(anchor(),'boot','ms-thor',101)==(1,2,0)


@pytest.mark.parametrize('now',[101, 100000, -100000])
def test_identified_same_boot_home_survives_elapsed_hour_and_clock_correction(now):
    a = dict(anchor(), machine_id='robot')
    assert validate_anchor(a,'boot','ms-thor',now,'robot') == (1,2,0)


@pytest.mark.parametrize('key,value',[
    ('machine_id','another_robot'), ('machine_id',None), ('boot_id','reboot'),
    ('host','another_host'), ('xy_yaw',[float('nan'),0,0]),
    ('created_unix_s',float('nan')),
])
def test_session_home_still_requires_identity_boot_and_valid_coordinates(key,value):
    a = dict(anchor(), machine_id='robot'); a[key] = value
    with pytest.raises((ReplayError,ValueError)):
        validate_anchor(a,'boot','ms-thor',100000,'robot')


def test_check_never_creates_publishers_without_explicit_enable():
    b=HomeBackend({})
    assert not b.commands_enabled
    with pytest.raises(ReplayError):b.send((.1,0,0))


def test_base_only_gid_is_sufficient_without_creating_arm_publishers():
    import time
    b=HomeBackend({});b._monitor_started_ns=time.monotonic_ns()
    assert not b.command_activity()['own_gids_bound']
    b._own_command_gids={'base_command':'ab'*16}
    assert b.command_activity()['own_gids_bound']


def test_vendor_yaw_w_is_not_interpreted_as_quaternion():
    from types import SimpleNamespace as N
    b=HomeBackend({})
    m=N(header=N(stamp=N(sec=1,nanosec=0),frame_id='base_link'),
        speed=N(linear=N(x=0.,y=0.),angular=N(z=0.)),
        odometry=N(position=N(x=1.,y=2.),orientation=N(w=.25)),
        is_soft_auto=False,is_hard_auto=True,is_parking_mode=False,is_avoid=False,
        is_joystick_on=False,is_handle_on=False)
    b._on_base_state(m)
    assert b.odometry['pose']==(1,2,.25)
    m.header.stamp.sec=0;m.header.stamp.nanosec=1
    b._on_base_state(m)
    assert b.odom_fault=='里程计时钟倒退'


def test_simple_xy_simulation_converges_without_reverse_trajectory():
    current=(1.2,-.4,.01); target=(0,0,0); previous=(0,0,0)
    for _ in range(2400):
        v=velocity(current,target,previous,.05)
        c,s=math.cos(current[2]),math.sin(current[2])
        current=(current[0]+(c*v[0]-s*v[1])*.05,current[1]+(s*v[0]+c*v[1])*.05,current[2])
        previous=v
        if v==(0,0,0):break
    assert math.hypot(*current[:2]) <= POLICY['position_tolerance_m']


def test_yaw_then_xy_recovers_reported_4f_failure_without_simultaneous_rotation_translation():
    from f1_replay.base_home import YawThenXY,YAW_POLICY
    current=(1.0524714099396841,.3338322360809406,-1.3229591310750528)
    target=(.5774789442643029,.2714120961462894,-1.2548912227880045)
    ctl=YawThenXY(current,target);previous=(0.,0.,0.);t=0.;phases=[]
    for _ in range(2400):
        t+=.05
        v=ctl.step(current,target,previous,.05,t,all(abs(x)<1e-8 for x in previous))
        assert not (abs(v[2])>0 and math.hypot(*v[:2])>0)
        assert abs(v[2]) <= YAW_POLICY['speed_rad_s']+1e-9
        phases.append(ctl.phase)
        c,s=math.cos(current[2]),math.sin(current[2])
        current=(current[0]+(c*v[0]-s*v[1])*.05,current[1]+(s*v[0]+c*v[1])*.05,current[2]+v[2]*.05)
        previous=v
        error=error_to_target(current,target)
        if ctl.phase=='return_xy' and math.hypot(*error[:2])<=POLICY['position_tolerance_m'] and abs(error[2])<=YAW_POLICY['tolerance_rad']:break
    assert 'align_yaw' in phases and 'return_xy' in phases
    assert t<120 and math.hypot(*error[:2])<=.025 and abs(error[2])<=math.radians(.5)


def test_yaw_controller_wraps_short_way_and_waits_for_standstill():
    from f1_replay.base_home import YawThenXY
    target=(1.,0.,-math.pi+.02);current=(0.,0.,math.pi-.02)
    c=YawThenXY(current,target)
    assert c.step(current,target,(0,0,0),.05,0,True)[2]>0
    aligned=(0,0,target[2])
    for t in [0,1,2]:assert c.step(aligned,target,(0,0,.01),.05,t,False)==(0,0,0)
    assert c.phase=='align_yaw'
    c.step(aligned,target,(0,0,0),.05,3,True)
    c.step(aligned,target,(0,0,0),.05,3.6,True)
    assert c.phase=='return_xy'


def test_yaw_rejects_large_initial_error_and_range():
    from f1_replay.base_home import YawThenXY
    with pytest.raises(ReplayError):YawThenXY((0,0,0),(0,0,math.radians(16)))
    with pytest.raises(ReplayError):YawThenXY((0,0,0),(4,0,0))


def test_small_yaw_residual_converges_with_observed_low_speed_stall():
    from f1_replay.base_home import YawThenXY,YAW_POLICY
    current=(.65427,.25497,-1.335176414323954)
    target=(*current[:2],-1.3526294887792154)
    ctl=YawThenXY(current,target);previous=(0.,0.,0.)
    for i in range(400):
        # Model a dead zone; this is a regression model, not hardware calibration.
        moving=abs(previous[2])>=.025
        v=ctl.step(current,target,previous,.05,i*.05,not moving)
        assert v[:2]==(0.,0.)
        actual=v[2] if abs(v[2])>=.025 else 0.
        current=(*current[:2],current[2]+actual*.05)
        previous=v
        if ctl.phase=='return_xy':break
    assert ctl.phase=='return_xy'
    assert abs(error_to_target(current,target)[2])<=YAW_POLICY['tolerance_rad']

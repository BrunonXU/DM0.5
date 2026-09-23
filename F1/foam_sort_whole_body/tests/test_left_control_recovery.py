import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import pytest


@pytest.mark.parametrize('hold_ok,service_ok,expected_calls,success',[
    (False,True,0,False),(True,False,1,False),(True,True,2,True)])
def test_recovery_requires_current_hold_and_checks_each_service(monkeypatch,tmp_path,hold_ok,service_ok,expected_calls,success):
    src=Path(__file__).resolve().parents[1]/'scripts/recover_left_control.py'
    spec=importlib.util.spec_from_file_location('left_recovery',src)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    srv=SimpleNamespace(Request=SimpleNamespace)
    monkeypatch.setitem(sys.modules,'f1_msgs',SimpleNamespace())
    monkeypatch.setitem(sys.modules,'f1_msgs.srv',SimpleNamespace(SetDragMode=srv,SetControlMode=srv))
    profile=tmp_path/'profile.json';profile.write_text(json.dumps({'expected_machine_id':Path('/etc/machine-id').read_text().strip()}))
    monkeypatch.setattr(module,'PROFILE',profile)
    out=tmp_path/'run';out.mkdir();monkeypatch.setattr(module,'create_run',lambda:out)
    from contextlib import nullcontext
    monkeypatch.setattr(module,'runtime_lock',lambda _:nullcontext())
    monkeypatch.setattr(module,'check_execution_host',lambda _:None)
    monkeypatch.setattr(module,'check_controller_parameters',lambda *_:None)
    monkeypatch.setattr(module,'wait_for_enter',lambda _:None)
    monkeypatch.setattr(module,'pump',lambda *_:None)
    monkeypatch.setattr(module,'stop_motion',lambda *_:dict(zero_velocity_feedback_confirmed=hold_ok,fresh_arm_hold_published=hold_ok))
    calls=[]
    class Backend:
        def __init__(self,*_):self._node=self
        def open(self):pass
        def check(self,**_):pass
        def poll(self,*_):pass
        def enable_commands(self):pass
        def close(self):pass
        def read_controller_parameters(self):return {'parameters':{}}
        def destroy_client(self,*_):pass
        def create_client(self,typ,name):
            def call(request):
                calls.append((name,vars(request)))
                return SimpleNamespace(done=lambda:True,result=lambda:SimpleNamespace(success=service_ok,message='fixture'))
            return SimpleNamespace(wait_for_service=lambda **_:True,call_async=call)
    monkeypatch.setattr(module,'RecoveryBackend',Backend)
    assert (module.main()==0)==success
    assert len(calls)==expected_calls
    if success:
        assert calls==[('/motor_ctl/set_drag_mode',{'target_arm':1,'drag_type':0,'duration_ms':0}),
                       ('/motor_ctl/set_control_mode',{'target_arm':1,'mode':1})]


def test_real_recovery_backend_creates_arm_publisher_and_can_hold(monkeypatch):
    from test_replay_ros_backend import FakeRos,profile
    from f1_replay.ros_backend import RosBackend,ARM_NAMES
    from f1_replay import command_monitor
    monkeypatch.setattr(command_monitor,'publisher_gid',lambda pub:b'fixture-gid')
    src=Path(__file__).resolve().parents[1]/'scripts/recover_left_control.py'
    spec=importlib.util.spec_from_file_location('left_recovery_publishers',src)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    ros=FakeRos()
    for key,value in ros.modules.items():monkeypatch.setitem(sys.modules,key,value)
    backend=module.RecoveryBackend(profile())
    try:
        # Exercise real command creation/hold methods; server subscription is
        # irrelevant to this regression and supplied by HomeBackend on robot.
        RosBackend.open(backend)
        backend._joints={name:{'position':float(i)} for i,name in enumerate(ARM_NAMES)}
        backend.enable_commands()
        assert set(backend._publishers)=={'arms_command','base_command'}
        record=backend.hold_arms()
        assert record['commands'][0]['status']=='sent'
        assert record['commands'][0]['fields']['position']==list(map(float,range(14)))
        assert backend.stop_base()['commands'][0]['status']=='sent'
    finally:backend.close()

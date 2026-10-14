import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from robot.command_activity import CommandActivity
from test_ros_adapter import Clock

class Monitor:
    def __init__(self,profile,name,cb):self.cb=cb;self.started_ns=1;self.fail=False
    def poll(self):
        if self.fail:raise RuntimeError('lost heartbeat')
    def close(self):pass

def watch():
    c=Clock();p={'topics':{'joint_command':'a'},'graph_observation_s':0};w=CommandActivity(p,'fixture',c,Monitor);return w,c

def test_idle_registered_publishers_are_allowed_but_actual_commands_block():
    w,c=watch();w.check('joint_command')
    w.record('arms_command','1'*32)
    with pytest.raises(RuntimeError,match='正在发送'):w.check('joint_command')
    c.now+=.6;w.check('joint_command')

def test_external_command_latches_during_action_and_own_gid_is_excluded():
    w,c=watch();w.bind('a'*32);w.arm('joint_command');w.record('arms_command','a'*32);w.check('joint_command')
    w.record('arms_command','b'*32);c.now+=1
    with pytest.raises(RuntimeError,match='执行期间'):w.check('joint_command')
    w.release('joint_command','a'*32);w.check('joint_command')

def test_monitor_failure_cannot_silently_enable_motion():
    w,c=watch();w.monitor.fail=True
    assert not w.snapshot()['ready']
    with pytest.raises(RuntimeError):w.check('base_command')

def test_retired_own_messages_do_not_poison_following_action():
    w,c=watch();w.bind('a'*32);w.release('joint_command','a'*32);w.record('arms_command','a'*32);w.check('joint_command')
    c.now+=3;w.record('arms_command','a'*32)
    with pytest.raises(RuntimeError):w.check('joint_command')

def test_commands_on_other_channel_do_not_mask_control_source_conflict():
    w,c=watch();w.arm('joint_command');w.record('base_command','b'*32)
    w.check('joint_command')
    with pytest.raises(RuntimeError):w.check('base_command')

def test_ros_loop_drains_monitor_without_browser():
    from types import SimpleNamespace
    import threading
    from robot.adapter import RosTransport
    transport=object.__new__(RosTransport)
    transport.stop=threading.Event();transport.context=SimpleNamespace(ok=lambda:True)
    counts={'drained':0}
    transport.store=SimpleNamespace(clock=lambda:1.,connected=True)
    transport.refresh_graph=lambda:None
    transport.executor=SimpleNamespace(spin_once=lambda **kw:transport.stop.set())
    transport.activity=SimpleNamespace(snapshot=lambda:counts.update(drained=counts['drained']+1))
    transport._spin()
    assert counts['drained']==1
    assert transport.store.connected is False

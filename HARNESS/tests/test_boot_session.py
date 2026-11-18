"""Request isolation of reusable ROS resources; all transports are fake."""
import sys
from types import SimpleNamespace as N
import pytest
from f1_harness.boot_capture import NativeBootSession,DEVICE_SERVICES
from test_boot_capture import info,joints


@pytest.fixture
def ros(monkeypatch):
    state=N(now=1_000_000_000,reply=True,samples=True,serial='first',calls=0,cancelled=0,
            nodes=0,destroyed=0,callbacks={},types=['test/Device'])
    class Context:
        active=False
        def ok(self):return self.active
    def init(*,args,context):context.active=True
    def shutdown(*,context):context.active=False
    class Future:
        def __init__(self):self.ready=state.reply;self.serial=state.serial
        def done(self):return self.ready
        def result(self):return dict(info=dict(serial_number=self.serial))
        def cancel(self):state.cancelled+=1
    class Client:
        def service_is_ready(self):return True
        def call_async(self,request):state.calls+=1;return Future()
        def remove_pending_request(self,future):pass
    class Node:
        def create_subscription(self,kind,topic,callback,qos):state.callbacks[topic]=callback;return topic
        def get_service_names_and_types(self):return [(v,state.types) for v in DEVICE_SERVICES.values()]
        def create_client(self,*args):return Client()
        def destroy_client(self,client):pass
        def destroy_node(self):state.destroyed+=1
    def create_node(*args,**kwargs):state.nodes+=1;return Node()
    class Executor:
        def __init__(self,**kwargs):pass
        def add_node(self,node):pass
        def shutdown(self,**kwargs):pass
        def spin_once(self,**kwargs):
            state.now+=100_000_000
            if state.samples:
                for topic,callback in state.callbacks.items():
                    message=joints() if topic=='/hal/joint_states' else info()
                    if topic=='/hal/joint_states':message.header.stamp=N(sec=state.now//10**9,nanosec=state.now%10**9)
                    callback(message)
    modules={'rclpy':N(init=init,shutdown=shutdown,create_node=create_node),
        'rclpy.context':N(Context=Context),'rclpy.executors':N(SingleThreadedExecutor=Executor),
        'rclpy.qos':N(qos_profile_sensor_data=object()),'sensor_msgs.msg':N(CameraInfo=object,JointState=object),
        'rosidl_runtime_py.utilities':N(get_service=lambda name:N(Request=object)),
        'rosidl_runtime_py.convert':N(message_to_ordereddict=lambda value:value)}
    for name,value in modules.items():monkeypatch.setitem(sys.modules,name,value)
    monkeypatch.setattr('f1_harness.boot_capture.time.monotonic_ns',lambda:state.now)
    profile=dict(cameras=[dict(name=c,rgb=dict(camera_info_topic='/'+c+'/rgb'),
        depth=dict(camera_info_topic='/'+c+'/depth')) for c in ('head','right')])
    with_session=NativeBootSession(profile)
    yield with_session,state
    with_session.close()


def test_reuses_discovery_but_requeries_serials_and_samples(ros):
    session,state=ros
    first=session.collect(seconds=1);state.serial='replacement'
    session.spin_idle(.01)
    second=session.collect(seconds=1)
    assert first['complete_window'] and second['complete_window']
    assert state.nodes==1 and state.calls==4
    assert first['camera_serials']==dict(head='first',right='first')
    assert second['camera_serials']==dict(head='replacement',right='replacement')
    assert first['capture_request_id']!=second['capture_request_id']
    assert first['capture_instance_id']==second['capture_instance_id']
    assert all(x['received_ns']>second['window_start_received_ns'] for x in second['joint_samples'])
    assert all(x['source_ns']>first['observed_monotonic_ns'] for x in second['joint_samples'])
    assert not second['motion_authorized'] and second['command_publishers']==0


@pytest.mark.parametrize('missing',['reply','samples'])
def test_successful_old_capture_cannot_satisfy_missing_new_evidence(ros,missing):
    session,state=ros
    assert session.collect(seconds=1)['complete_window']
    setattr(state,missing,False)
    failed=session.collect(seconds=1)
    assert not failed['complete_window'] and failed['errors']
    if missing=='reply':assert failed['device_info']=={} and state.cancelled==2
    else:assert failed['joint_samples']==[]
    setattr(state,missing,True)
    assert session.collect(seconds=1)['complete_window']


def test_device_service_type_change_requires_new_session(ros):
    session,state=ros
    assert session.collect(seconds=1)['complete_window']
    state.types=['test/ChangedDevice']
    result=session.collect(seconds=1)
    assert not result['complete_window'] and 'type_changed' in result['errors'][0]


def test_serialization_and_shutdown(ros):
    session,state=ros
    session.lock.acquire()
    try:
        with pytest.raises(RuntimeError,match='busy'):session.collect(seconds=1)
    finally:session.lock.release()
    session.close();session.close()
    assert state.destroyed==1
    with pytest.raises(RuntimeError,match='closed'):session.collect(seconds=1)

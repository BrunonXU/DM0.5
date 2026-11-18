from types import SimpleNamespace as Namespace
import sys

from f1_harness.ros_observer import RosObserver


def test_image_callback_requires_message_info_and_rebuilds_publisher_identity():
    import inspect
    import pytest
    from f1_harness.observation import ObservationBuffer
    from f1_harness.evidence import FrameCache
    observer=object.__new__(RosObserver)
    observer.buffer=ObservationBuffer();observer.store=FrameCache()
    observer.publishers={};observer.retired_publishers={};observer.info={};observer.sequences={}
    callback=observer._image_callback(dict(name='head',source_clock_domain='camera'),'rgb',
        dict(type='raw',width=2,height=2))
    with pytest.raises(TypeError):inspect.signature(callback).bind(object())
    inspect.signature(callback).bind(object(),object())
    msg=Namespace(header=Namespace(stamp=Namespace(sec=1,nanosec=0),frame_id='head'),
        width=2,height=2,encoding='rgb8',step=6,is_bigendian=False,data=bytes(12))
    callback(msg,Namespace(publisher_gid=bytes([1])*16))
    original=observer.buffer.snapshot()
    assert original.frames[0].publisher_id=='01'*16
    msg.header.stamp.sec=0;msg.header.stamp.nanosec=1
    callback(msg,dict(publisher_gid=bytes([2])*16))
    latest=observer.buffer.snapshot()
    assert latest.acquisition_instance!=original.acquisition_instance
    assert latest.frames[0].source_epoch==1 and latest.frames[0].source_ns==1
    assert latest.frames[0].publisher_id=='02'*16
    callback(msg,Namespace(publisher_gid=bytes([1])*16))
    assert 'multiple_source_publishers:head' in observer.buffer.snapshot().faults


def test_observer_private_executor_and_idempotent_close(monkeypatch):
    calls=[]
    class Context:
        active=False
        def ok(self):return self.active
    class ExternalShutdown(Exception):pass
    class Node:
        def destroy_node(self):calls.append('node_destroy')
    node=Node()
    def init(*,args,context):context.active=True;calls.append(('init',context))
    def create_node(name,*,context):
        assert context is calls[0][1]
        return node
    def shutdown(*,context):context.active=False;calls.append('context_shutdown')
    class Executor:
        def __init__(self,*,context):assert context is calls[0][1]
        def add_node(self,value):assert value is node
        def spin(self):calls.append('explicit_spin');raise ExternalShutdown()
        def shutdown(self,*,timeout_sec):calls.append('executor_shutdown')
    modules={
        'rclpy':Namespace(init=init,create_node=create_node,shutdown=shutdown),
        'rclpy.context':Namespace(Context=Context),
        'rclpy.executors':Namespace(SingleThreadedExecutor=Executor,ExternalShutdownException=ExternalShutdown),
        'rclpy.qos':Namespace(qos_profile_sensor_data=object()),
        'sensor_msgs.msg':Namespace(CameraInfo=object,JointState=object,Image=object,CompressedImage=object)}
    for name,value in modules.items():monkeypatch.setitem(sys.modules,name,value)
    observer=RosObserver({'cameras':[]},Namespace())
    observer.spin();observer.close();observer.close()
    assert calls[1:]==['explicit_spin','executor_shutdown','node_destroy','context_shutdown']

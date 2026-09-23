"""Exercise actual publish wrapping with ROS stubbed; no middleware required."""
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from pico_upper.contract import ARM_NAMES

class FakeNode:
    def __init__(self, name):
        self.raw=[]
        self.create_publisher(object,'/parameter_events',10)
    def create_publisher(self, kind, topic, qos, **kwargs):
        result=SimpleNamespace(messages=[])
        result.publish=lambda msg: result.messages.append(msg)
        self.raw.append((topic,result))
        return result

class GuardTest(unittest.TestCase):
    def setUp(self):
        fake=ModuleType('rclpy.node'); fake.Node=FakeNode
        path=Path(__file__).resolve().parents[1]/'src/pico_upper/ros_guard.py'
        spec=importlib.util.spec_from_file_location('isolated_upper_ros_guard',path)
        self.module=importlib.util.module_from_spec(spec)
        with patch.dict('sys.modules',{'rclpy.node':fake}):
            spec.loader.exec_module(self.module)
        self.node=self.module.GuardedNode('test')
    def test_ros_metadata_initializes_but_body_publisher_is_denied(self):
        self.assertEqual(self.node.raw[0][0],'/parameter_events')
        with self.assertRaises(RuntimeError):self.node.create_publisher(object,'/move/ManualMoveCmd',10)
        self.assertEqual(len(self.node.raw),1)
    def test_nonarm_message_cannot_pass_shared_joint_topic(self):
        pub=self.node.create_publisher(object,'/motion_ctl/joint_ctl',1)
        raw=self.node.raw[-1][1]
        with self.assertRaises(ValueError):pub.publish(SimpleNamespace(name=['body_j3'],position=[-10.]))
        self.assertFalse(raw.messages)
        pub.publish(SimpleNamespace(name=ARM_NAMES,position=[0.]*14))
        self.assertEqual(len(raw.messages),1)
    def test_service_calls_are_denied(self):
        with self.assertRaises(RuntimeError):self.node.create_client(object,'/move/SetMode')
if __name__=='__main__':unittest.main()

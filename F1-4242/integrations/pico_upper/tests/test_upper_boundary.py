import ast
import json
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest

from pico_upper.contract import ARM_NAMES, decode_frame, validate_frame, validate_message, validate_profile
ROOT = Path(__file__).resolve().parents[1]

class OutputBoundaryTests(unittest.TestCase):
    def test_valid_approved_frame(self):
        frame = [0.]*14+[50.,50.,1.,0.]
        self.assertEqual(decode_frame(struct.pack('<18d',*frame),('127.0.0.1',5)),tuple(frame))
    def test_shortcut_packets_never_reach_output(self):
        for values in ([1.],[2.],[3.],[1.,123.],[0.]*14,[0.]*16):
            with self.assertRaises(ValueError):
                decode_frame(struct.pack('<'+'d'*len(values),*values),('127.0.0.1',5))
    def test_nonlocal_invalid_and_nonfinite_rejected(self):
        frame = [0.]*14+[50.,50.,1.,0.]
        with self.assertRaises(ValueError): decode_frame(struct.pack('<18d',*frame),('192.0.2.3',5))
        for index,value in [(0,float('nan')),(3,float('inf')),(14,101),(16,.5)]:
            invalid=frame.copy(); invalid[index]=value
            with self.assertRaises(ValueError): validate_frame(invalid)
    def test_body_or_reordered_commands_rejected(self):
        for names in [list(ARM_NAMES)+['body_j3'],list(reversed(ARM_NAMES)),['body_j3']]:
            with self.assertRaises(ValueError):
                validate_message('/motion_ctl/joint_ctl',SimpleNamespace(name=names,position=[0.]*len(names)))
        validate_message('/motion_ctl/joint_ctl',SimpleNamespace(name=ARM_NAMES,position=[0.]*14))
    def test_base_waist_topics_rejected(self):
        for topic in ['/move/ManualMoveCmd','/control/waist/updown','/pico/chassis_enabled']:
            with self.assertRaises(ValueError): validate_message(topic,SimpleNamespace())
    def test_config_cannot_enable_nonupper_capabilities(self):
        config=json.loads((ROOT/'config/control.json').read_text())
        validate_profile(config)
        for field in ['chassis','torso_pitch','recording']:
            bad=json.loads(json.dumps(config));bad[field]['enabled']=True
            with self.assertRaises(ValueError): validate_profile(bad)
    def test_disabled_methods_have_no_executable_output(self):
        path=ROOT/'vendor/xrobotoolkit/xrobotoolkit_teleop/common/base_teleop_controller_ros2_mixin.py'
        tree=ast.parse(path.read_text())
        names={'_publish_chassis_command','_publish_chassis_command_unlocked','_chassis_publish_loop',
               '_publish_emergency_chassis_zero','_update_torso_target','_publish_approved_torso_position','_torso_output_loop'}
        found=set()
        for n in ast.walk(tree):
            if isinstance(n,ast.FunctionDef) and n.name in names:
                found.add(n.name)
                self.assertFalse(any(isinstance(x,ast.Call) for x in ast.walk(n)),n.name)
        self.assertEqual(found,names)
    def test_teleop_no_forbidden_publisher_or_subscription(self):
        p=ROOT/'vendor/xrobotoolkit/xrobotoolkit_teleop/common/base_teleop_controller.py'
        tree=ast.parse(p.read_text())
        publishers=[]
        for n in ast.walk(tree):
            if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='create_publisher':
                publishers.append(ast.literal_eval(n.args[1]))
        self.assertEqual(set(publishers),{'/lead/joint_states','/lead/left_tcp','/lead/right_tcp',
                         '/motion_ctl/gripper/left','/motion_ctl/gripper/right'})
        self.assertNotIn('TorsoOutput',p.read_text())
        self.assertNotIn('ChassisOutput',p.read_text())
    def test_no_collection_or_reset_in_menu(self):
        tree=ast.parse((ROOT/'src/pico_upper/controls.py').read_text())
        strings=[n.value for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str)]
        self.assertIn('left_menu_button',strings)
        self.assertFalse(set('ABXY') & set(strings))
    def test_collision_failure_explicitly_blocks_output(self):
        source=(ROOT/'vendor/xrobotoolkit/xrobotoolkit_teleop/common/base_teleop_controller.py').read_text()
        self.assertIn('if self._safety._collision_guard is None:',source)
        self.assertLess(source.index('COLLISION_GUARD_UNAVAILABLE'),source.index('self._start_collision_detection_thread()'))

if __name__=='__main__':unittest.main()

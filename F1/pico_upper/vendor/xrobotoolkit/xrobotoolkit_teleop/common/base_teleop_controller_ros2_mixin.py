"""
ROS2 Mixin for BaseTeleopController.
Contains all ROS2-related methods: spin, callbacks, publish loop, and destructor.
Split from base_teleop_controller.py to reduce file size.
"""

import threading
import time
import json
from contextlib import nullcontext

import numpy as np
import meshcat.transformations as tf

import rclpy
import rclpy.executors
from rclpy.node import Node
from sensor_msgs.msg import JointState
from control_msgs.msg import GripperCommand
from geometry_msgs.msg import TwistStamped
from std_msgs.msg import Bool, String
from _safety_protector import ALL_JOINT_NAMES

from xrobotoolkit_teleop.utils.geometry import (
    apply_delta_pose,
    quat_diff_as_angle_axis, 
)

test_num = 0  # 新增：测试用计数器


class Ros2TeleopMixin:
    """ROS2 相关方法的 Mixin 类，由 BaseTeleopController 继承使用。
    所有 self.xxx 属性由 BaseTeleopController.__init__ 初始化。"""

    def _ros_spin(self):
        # 等待 _ros_node 被创建（最多等 5 秒）
        for _ in range(500):
            if hasattr(self, '_ros_node'):
                break
            time.sleep(0.01)

        executor = rclpy.executors.SingleThreadedExecutor()
        executor.add_node(self._ros_node)
        try:
            while not self._stop_event.is_set() and rclpy.ok():
                executor.spin_once(timeout_sec=0.05)
        except rclpy.executors.ExternalShutdownException:
            pass
        finally:
            try:
                executor.remove_node(self._ros_node)
            except Exception:
                pass

    def _replay_mode_callback(self, msg: Bool):
        print("  _replay_mode_callback ",msg)
        """切换回放模式"""
        self.tcp_replay_mode = msg.data
        if self.tcp_replay_mode:
            print("===== TCP REPLAY MODE ON =====")
        else:
            print("===== TCP REPLAY MODE OFF =====")


    def _hal_joint_states_callback(self, msg: JointState):
        """Continuously refresh full-body feedback used by collision checking."""
        if len(msg.name) != len(msg.position) or len(set(msg.name)) != len(msg.name):
            return
        with self._hal_joint_states_lock:
            first_sample = not self._hal_joint_states_received
            for name, value in zip(msg.name, msg.position):
                if np.isfinite(value):
                    self._hal_joint_position_by_name[name] = float(value)
                    self._hal_joint_seen[name] = time.monotonic()
            now = time.monotonic()
            complete = all(name in self._hal_joint_position_by_name and now - self._hal_joint_seen.get(name, 0.0) <= float(self._control_config["feedback_timeout_s"]) for name in ALL_JOINT_NAMES)
            if complete:
                ordered = [self._hal_joint_position_by_name[name] for name in ALL_JOINT_NAMES]
                self.hal_full_body_joint_states[:22] = ordered
                self._hal_joint_states_received = True
            if complete:
                self._hal_joint_states_last_monotonic = time.monotonic()
        if first_sample and complete:
            print(
                f"[INFO] /hal/joint_states named safety feedback active "
                f"({len(self._hal_joint_position_by_name)} joints cached)"
            )

    def _chassis_feedback_callback(self, msg):
        """Cache actual velocity and odometry for closed-loop pose following."""
        speed = [
            float(msg.speed.linear.x),
            float(msg.speed.linear.y),
            float(msg.speed.angular.z),
        ]
        # F1 move/msg/State documents orientation.w as yaw in radians; it is
        # not a geometry quaternion on this supplier interface.
        pose = [
            float(msg.odometry.position.x),
            float(msg.odometry.position.y),
            float(msg.odometry.orientation.w),
        ]
        if not np.all(np.isfinite(speed)) or not np.all(np.isfinite(pose)):
            return
        with self._chassis_feedback_lock:
            self._chassis_feedback_speed = speed
            self._chassis_feedback_pose = pose
            self._chassis_feedback_monotonic = time.monotonic()

    def _publish_chassis_command(self, *args, **kwargs):
        """Disabled in upper-only build; never create or publish a command."""
        return None

    def _publish_chassis_command_unlocked(self, *args, **kwargs):
        """Disabled in upper-only build; never create or publish a command."""
        return None

    def _chassis_publish_loop(self, *args, **kwargs):
        """Disabled in upper-only build; never create or publish a command."""
        return None

    def _publish_emergency_chassis_zero(self, *args, **kwargs):
        """Disabled in upper-only build; never create or publish a command."""
        return None

    def _deactivate_torso_command(self, measured_pitch=None):
        """Stop publishing body_j3 and discard every prior activation target."""
        self._torso_mapper.deactivate()
        self._torso_last_timestamp = None
        self._torso_last_fresh_monotonic = 0.0
        with self._trajectory_lock:
            with self._both_arm_grip_lock:
                self._raw_torso_pitch = None
                self._torso_command_active = False
            with self._safety_lock:
                if measured_pitch is None:
                    self._torso_trajectory.hold()
                else:
                    pitch = float(measured_pitch)
                    self._torso_trajectory.reset([pitch])
                    self._last_safe_torso_pitch = pitch
        if hasattr(self, "_torso_output_lock"):
            with self._torso_output_lock:
                self._torso_output_active = False
                if measured_pitch is not None:
                    pitch = float(measured_pitch)
                    self._torso_output_trajectory.reset([pitch])
                    self._torso_output_target = pitch

    def _update_torso_target(self, *args, **kwargs):
        """Disabled in upper-only build; never create or publish a command."""
        return None

    def _publish_approved_torso_position(self, *args, **kwargs):
        """Disabled in upper-only build; never create or publish a command."""
        return None

    def _torso_output_loop(self, *args, **kwargs):
        """Disabled in upper-only build; never create or publish a command."""
        return None

    def _external_left_pose_callback(self,msg):
        if not self.tcp_replay_mode:
            return
        self._update_robot_state()
        self.placo_robot.update_kinematics()
        frame_flag = msg.header.frame_id
        xr_pose = list(msg.position)
        src_name = "left_hand"
        config = self.manipulator_config[src_name] 

        # 相当于按下遥控键
        if frame_flag == "press_pico":
            if self.ref_ee_xyz[src_name] is None :
                print(f"{src_name} is activated.")
                self.ref_ee_xyz[src_name], self.ref_ee_quat[src_name] = self._get_link_pose(config["link_name"])
            quat_back = self.euler_deg_to_quat([xr_pose[3],xr_pose[4],xr_pose[5]])
            xr_pose_now = [xr_pose[0],xr_pose[1],xr_pose[2], quat_back[1],quat_back[2],quat_back[3],quat_back[0]]
            delta_xyz, delta_rot = self._process_xr_pose(xr_pose_now, src_name)
            # print("delta_xyz,delta_rot", delta_xyz,delta_rot)
            #print("self.ref_ee_xyz[src_name]",src_name,self.ref_ee_xyz[src_name])
            if self.effector_control_mode[src_name] == "position":
                # Position-only control: only apply position delta
                target_xyz = self.ref_ee_xyz[src_name] + delta_xyz
                self.effector_task[src_name].target_world = target_xyz
            else:
                # Full pose control: apply both position and orientation deltas
                target_xyz, target_quat = apply_delta_pose(
                    self.ref_ee_xyz[src_name],
                    self.ref_ee_quat[src_name],
                    delta_xyz,
                    delta_rot,
                )
                target_pose = tf.quaternion_matrix(target_quat)
                target_pose[:3, 3] = target_xyz
                self.effector_task[src_name].T_world_frame = target_pose


            l_ready_send = self.placo_robot.state.q[12:19].copy() - self.left_q_delta_init_and_solver
            r_ready_send = self.placo_robot.state.q[19:26].copy() -self.right_q_delta_init_and_solver
            l_real,r_real = self.real_arm_command_sender.angle_map(l_ready_send.tolist(), r_ready_send.tolist())

            if(self.xy_pressed_flag_once):
                # print("xy_pressed_flag_once")
                self.origin_begin_flag_once = False
                self.reset_delta_left = np.array(l_real - self.left_q_reset)
                self.reset_delta_right = np.array(r_real - self.right_q_reset)

                #bu neng yi zhi jin ru zhe li,fou ze yi zhi geng xin self.reset_delta_left,self.reset_delta_right
                self.xy_pressed_flag_once = False
                self.reset_already = True
                self.origin_begin_flag = False

            if self.reset_already:
                # print("reset_already")
                l_final = l_real.copy() - self.reset_delta_left
                r_final = r_real.copy() - self.reset_delta_right

            if(self.origin_begin_flag_once):
                # print("origin_begin_flag_once")
                self.origin_delta_left = np.array(l_real.copy() - self.left_q_origin)
                self.origin_delta_right = np.array(r_real.copy() - self.right_q_origin)
                self.origin_begin_flag = True
                self.origin_begin_flag_once = False
            
            if(self.origin_begin_flag):
                # print("origin_begin_flag")
                l_final = l_real.copy() - self.origin_delta_left
                r_final = r_real.copy() - self.origin_delta_right
            
            
            both_arm_grip = l_final.tolist() + r_final.tolist()
            both_arm_grip = self._stage_and_get_safe_arm_command(both_arm_grip)
            # time.sleep(0.2)
            self.real_arm_command_sender.send_upper_to_sim(both_arm_grip)
            print("left_write_sucess")

        else:
            if self.ref_ee_xyz[src_name] is not None:
                print(f"{src_name} is deactivated.")
                self.ref_ee_xyz[src_name] = None
                self.ref_controller_xyz[src_name] = None

            if hasattr(self, 'joints_regularization_task'):
                    self.joints_regularization_task.set_joints(self._get_joint_values_dict())
        self._update_motion_tracker_tasks()
        try:
            self.solver.solve(True)
        except RuntimeError as e:
            print(f"IK solver failed: {e}")


    def _external_right_pose_callback(self,msg):
        if not self.tcp_replay_mode:
            return
        self._update_robot_state()
        self.placo_robot.update_kinematics()
        frame_flag = msg.header.frame_id
        xr_pose = list(msg.position)
        src_name = "right_hand"
        config = self.manipulator_config[src_name] 

        # 相当于按下遥控键
        if frame_flag == "press_pico":
            if self.ref_ee_xyz[src_name] is None :
                print(f"{src_name} is activated.")
                self.ref_ee_xyz[src_name], self.ref_ee_quat[src_name] = self._get_link_pose(config["link_name"])
            quat_back = self.euler_deg_to_quat([xr_pose[3],xr_pose[4],xr_pose[5]])
            xr_pose_now = [xr_pose[0],xr_pose[1],xr_pose[2], quat_back[1],quat_back[2],quat_back[3],quat_back[0]]
            delta_xyz, delta_rot = self._process_xr_pose(xr_pose_now, src_name)
            # print("delta_xyz,delta_rot", delta_xyz,delta_rot)
            #print("self.ref_ee_xyz[src_name]",src_name,self.ref_ee_xyz[src_name])
            if self.effector_control_mode[src_name] == "position":
                # Position-only control: only apply position delta
                target_xyz = self.ref_ee_xyz[src_name] + delta_xyz
                self.effector_task[src_name].target_world = target_xyz
            else:
                # Full pose control: apply both position and orientation deltas
                target_xyz, target_quat = apply_delta_pose(
                    self.ref_ee_xyz[src_name],
                    self.ref_ee_quat[src_name],
                    delta_xyz,
                    delta_rot,
                )
                target_pose = tf.quaternion_matrix(target_quat)
                target_pose[:3, 3] = target_xyz
                self.effector_task[src_name].T_world_frame = target_pose


            l_ready_send = self.placo_robot.state.q[12:19].copy() - self.left_q_delta_init_and_solver
            r_ready_send = self.placo_robot.state.q[19:26].copy() -self.right_q_delta_init_and_solver
            l_real,r_real = self.real_arm_command_sender.angle_map(l_ready_send.tolist(), r_ready_send.tolist())

            if(self.xy_pressed_flag_once):
                # print("xy_pressed_flag_once")
                self.origin_begin_flag_once = False
                self.reset_delta_left = np.array(l_real - self.left_q_reset)
                self.reset_delta_right = np.array(r_real - self.right_q_reset)

                #bu neng yi zhi jin ru zhe li,fou ze yi zhi geng xin self.reset_delta_left,self.reset_delta_right
                self.xy_pressed_flag_once = False
                self.reset_already = True
                self.origin_begin_flag = False

            if self.reset_already:
                # print("reset_already")
                l_final = l_real.copy() - self.reset_delta_left
                r_final = r_real.copy() - self.reset_delta_right

            if(self.origin_begin_flag_once):
                # print("origin_begin_flag_once")
                self.origin_delta_left = np.array(l_real.copy() - self.left_q_origin)
                self.origin_delta_right = np.array(r_real.copy() - self.right_q_origin)
                self.origin_begin_flag = True
                self.origin_begin_flag_once = False
            
            if(self.origin_begin_flag):
                # print("origin_begin_flag")
                l_final = l_real.copy() - self.origin_delta_left
                r_final = r_real.copy() - self.origin_delta_right
            
            
            both_arm_grip = l_final.tolist() + r_final.tolist()
            both_arm_grip = self._stage_and_get_safe_arm_command(both_arm_grip)
            # time.sleep(0.2)
            self.real_arm_command_sender.send_upper_to_sim(both_arm_grip)
            print("right_write_sucess")

        else:

            if self.ref_ee_xyz[src_name] is not None:
                print(f"{src_name} is deactivated.")
                self.ref_ee_xyz[src_name] = None
                self.ref_controller_xyz[src_name] = None

            if hasattr(self, 'joints_regularization_task'):
                    self.joints_regularization_task.set_joints(self._get_joint_values_dict())
        self._update_motion_tracker_tasks()
        try:
            self.solver.solve(True)
        except RuntimeError as e:
            print(f"IK solver failed: {e}")

    # ==================== 新增：50Hz 发布循环 ====================
    def _publish_loop(self):
        """Publish only CollisionGuard-approved, jerk-limited arm targets."""
        period = 1.0 / float(self._control_config["command_hz"])
        while not self._pub_thread_stop.is_set() and rclpy.ok():
            loop_start = time.time()
            
            # 【关键】还没收到过数据，跳过本次发布
            with self._both_arm_grip_lock:
                if self._both_arm_grip_updated and not self._reset_in_progress.is_set():
                    # 构造 JointState 消息
                    msg = JointState()
                    msg.header.stamp = self._ros_node.get_clock().now().to_msg()
                    msg.name = self._pub_lead_joint_names
                    
                    data = list(self._latest_both_arm_grip)
                    # Arm commands are released only after the independent
                    # CollisionGuard thread has copied them into last_safe.
                    # This also closes the one-cycle race between a new XR
                    # target and the asynchronous safety evaluation.
                    with self._safety_lock:
                        safe_left = self._last_safe_left.copy()
                        safe_right = self._last_safe_right.copy()
                    data[0:7] = safe_left.tolist()
                    data[7:14] = safe_right.tolist()
                    data.pop(15)
                    data.pop(14)
                    msg.position = data

                    try:
                        self._joint_state_pub.publish(msg)
                    except Exception:
                        if not rclpy.ok():
                            return
                        raise
                    #print("self._joint_state_pub.publish_____sucess")


                    # 提取夹爪数据（index 14 = 左夹爪, index 15 = 右夹爪）
                    grip_data = list(self._latest_both_arm_grip)
                    left_grip_val = grip_data[14]
                    right_grip_val = grip_data[15]

                    # 发布左夹爪 GripperCommand
                    l_grip_msg = GripperCommand()
                    l_grip_msg.position = float(left_grip_val)
                    l_grip_msg.max_effort = float(
                        self._control_config["gripper_max_effort"]
                    )
                    if self._raw_arm_enabled[0]:
                        self._gripper_left_pub.publish(l_grip_msg)

                    # 发布右夹爪 GripperCommand
                    r_grip_msg = GripperCommand()
                    r_grip_msg.position = float(right_grip_val)
                    r_grip_msg.max_effort = float(
                        self._control_config["gripper_max_effort"]
                    )
                    if self._raw_arm_enabled[1]:
                        self._gripper_right_pub.publish(r_grip_msg)


            with self.pico_tcp_data_lock:
                if self.pico_left_tcp_data_updated:
                    l_msg = JointState()
                    l_msg.header.frame_id = "press_pico" if self.latest_left_active else "un_press_pico"
                    l_msg.header.stamp = self._ros_node.get_clock().now().to_msg()
                    l_msg.name = self.pico_tcp_pos_names
                    
                    l_msg.position = list(self.latest_left_tcp_pos)
                    self.pico_left_tcp_pub.publish(l_msg)
                    #print("self.pico_left_tcp_pub.publish_____sucess")
                if self.pico_right_tcp_data_updated:
                    r_msg = JointState()
                    r_msg.header.stamp = self._ros_node.get_clock().now().to_msg()
                    r_msg.name = self.pico_tcp_pos_names
                    r_msg.header.frame_id = "press_pico" if self.latest_right_active else "un_press_pico"
                    r_msg.position = list(self.latest_right_tcp_pos)
                    self.pico_right_tcp_pub.publish(r_msg)
                    #print("self.pico_right_tcp_pub.publish_______sucess")

            # Chassis runs on its own worker, never behind these arm/TCP locks
            # or _update_torso_target's trajectory/collision lock.

            # A hold stages body_j3 from headset pitch. Only the independently
            # collision-approved point is released to the motor topic.
            self._update_torso_target()


            elapsed = time.time() - loop_start
            sleep_time = period - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    def _update_latest_both_arm_grip(self, both_arm_grip):
        """线程安全地更新最新数据"""
        with self._both_arm_grip_lock:
            self._latest_both_arm_grip = list(both_arm_grip)

            if not self._both_arm_grip_updated:
                self._both_arm_grip_updated = True
                print("[ROS2] First data received...")

    def _update_latest_pico_tcp_data(self, tcp_data, which_side, active_flag):
        with self.pico_tcp_data_lock:
            if which_side == "left":
                self.latest_left_tcp_pos = list(tcp_data)
                self.latest_left_active = active_flag      # 新增：保存激活状态
                if not self.pico_left_tcp_data_updated:
                    self.pico_left_tcp_data_updated = True
            elif which_side == "right":
                self.latest_right_tcp_pos = list(tcp_data)
                self.latest_right_active = active_flag
                if not self.pico_right_tcp_data_updated:
                    self.pico_right_tcp_data_updated = True


    # ==================== 新增：析构时清理 ROS2 ====================
    def __del__(self):
        """清理 ROS2 资源"""
        if hasattr(self, '_chassis_worker_stop'):
            self._chassis_worker_stop.set()
        if hasattr(self, '_chassis_worker_thread') and self._chassis_worker_thread.is_alive():
            self._chassis_worker_thread.join(timeout=1.0)
        if hasattr(self, '_torso_output_stop'):
            self._torso_output_stop.set()
        if hasattr(self, '_torso_output_thread') and self._torso_output_thread.is_alive():
            self._torso_output_thread.join(timeout=1.0)
        if hasattr(self, '_safety_thread_stop'):
            self._safety_thread_stop.set()   # 新增
        if hasattr(self, '_collision_thread') and self._collision_thread.is_alive():
            self._collision_thread.join(timeout=1.0)

        if hasattr(self, '_pub_thread_stop'):
            self._pub_thread_stop.set()
        if hasattr(self, '_pub_thread') and self._pub_thread.is_alive():
            self._pub_thread.join(timeout=1.0)
        self._publish_emergency_chassis_zero()
        if hasattr(self, '_ros_node'):
            self._ros_node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    # ==================== 新增结束 ====================
    def _recording_status_callback(self, message):
        """Make button arbitration follow the recorder's authoritative state."""
        mode = str(message.data).strip()
        if mode not in {"idle", "recording", "finalizing", "review", "recovery"}:
            print(f"[RECORDING] ignored invalid recorder status: {mode!r}")
            return
        previous = self._recording_state
        self._recording_state = mode
        self.is_collecting = mode == "recording"
        if mode == "review" and previous != "review":
            self._review_button_arbiter.reset()
        if mode != previous:
            print(f"LEROBOT_RECORDER_STATUS {previous}->{mode}")

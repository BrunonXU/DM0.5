"""
IK & Visualization Mixin for BaseTeleopController.
Contains IK update methods, visualization, and quaternion/euler conversion utilities.
Split from base_teleop_controller.py to reduce file size.
"""

import time
import webbrowser

import meshcat.transformations as tf
import numpy as np
from placo_utils.visualization import (
    frame_viz,
    robot_frame_viz,
    robot_viz,
)

from xrobotoolkit_teleop.utils.geometry import (
    apply_delta_pose,
    quat_diff_as_angle_axis,
)


class IkVizMixin:
    """IK 求解、可视化和坐标转换相关方法的 Mixin 类。
    所有 self.xxx 属性由 BaseTeleopController.__init__ 初始化。"""

    def _handle_xr_connection(self):
        """Hold on loss; rebase at measured joints before accepting a new Grip.

        The session X+Y home snapshot is preserved. This never sends home/reset.
        """
        if not hasattr(self.xr_client, 'input_ready'):
            return False
        if not self.xr_client.input_ready():
            self._xr_rebase_pending = True
            for refs in (self.ref_ee_xyz, self.ref_ee_quat,
                         self.ref_controller_xyz, self.ref_controller_quat):
                for name in refs:
                    refs[name] = None
            for name in self.active:
                self.active[name] = False
            return True
        generation = getattr(self.xr_client, 'recovery_generation', 0)
        if (not getattr(self, '_xr_rebase_pending', False)
                and getattr(self, '_xr_generation_seen', 0) == generation):
            return False
        with self._hal_joint_states_lock:
            measured = self.hal_full_body_joint_states[:14].copy()
            age = time.monotonic() - self._hal_joint_states_last_monotonic
        if age > self._feedback_timeout_s or not np.all(np.isfinite(measured)):
            return True
        with self._trajectory_lock:
            # Reuse the existing hardware->Placo map without changing the
            # operator's per-launch reset pose or calibration offsets.
            left_origin, right_origin = self.left_q_origin, self.right_q_origin
            try:
                self.left_q_origin = measured[:7].tolist()
                self.right_q_origin = measured[7:].tolist()
                self.sync_placo_to_real(False)
            finally:
                self.left_q_origin, self.right_q_origin = left_origin, right_origin
            left = self.placo_robot.state.q[12:19] - self.left_q_delta_init_and_solver
            right = self.placo_robot.state.q[19:26] - self.right_q_delta_init_and_solver
            l_real, r_real = self.real_arm_command_sender.angle_map(left.tolist(), right.tolist())
            self.origin_delta_left = np.asarray(l_real) - measured[:7]
            self.origin_delta_right = np.asarray(r_real) - measured[7:]
            self.origin_begin_flag = True
            self.origin_begin_flag_once = False
            self.xy_pressed_flag_once = False
            self.reset_already = False
            self._finish_motion_reset(measured)
        self._xr_rebase_pending = False
        self._xr_generation_seen = generation
        print('XR_RECONNECTED_REBASE_OK measured joints; session X+Y home unchanged')
        return True

    def _handle_local_frame_recenter(self):
        """Re-clutch both arms after an operator-requested frame reset."""
        with self._local_frame_lock:
            generation = self._local_frame_generation
        if generation == self._arm_local_frame_generation_seen:
            return
        self.sync_end_effector_poses_to_placo_tasks()
        with self._trajectory_lock:
            self._arm_clutch.release([False, False])
            self._raw_arm_enabled = [False, False]
            self._trajectory.hold()
        for name in self.manipulator_config:
            self.ref_ee_xyz[name] = None
            self.ref_ee_quat[name] = None
            self.ref_controller_xyz[name] = None
            self.ref_controller_quat[name] = None
        self._arm_local_frame_generation_seen = generation
        print(f"LOCAL_FRAME_RECENTER_ARMS_OK generation={generation}")

    def _handle_xy_home_reset(self):
        """XY whole-body reset is unavailable in the upper-only trial."""
        self._reset_request.clear()
        return False

    def _update_ik_lxl(self):
        """
        This is the core IK logic block. It reads from XR, updates Placo tasks,
        and solves the kinematics.
        """

        self._handle_local_frame_recenter()
        if self._handle_xy_home_reset():
            return

        self._update_robot_state()
        self.placo_robot.update_kinematics()
        # print("updata_ik之后，关节角度\n")
        # print("left_arm_state\n",self.placo_robot.state.q[12:19])
        # print("right_arm_state\n",self.placo_robot.state.q[19:26])
        for src_name, config in self.manipulator_config.items():

            # ===================================================

            # print("debug\n---------------\n",src_name,config)
            xr_grip_val = self.xr_client.get_key_value_by_name(config["control_trigger"])
            # print(f"Grip value for {src_name}: {xr_grip_val}")
            self.active[src_name] = xr_grip_val > 0.9
            # print(f"{src_name} active: {self.active[src_name]}")
            if self.active[src_name]:
                if self.ref_ee_xyz[src_name] is None :
                    print(f"{src_name} is activated.")
                    self.ref_ee_xyz[src_name], self.ref_ee_quat[src_name] = self._get_link_pose(config["link_name"])
                    print(f"[DEBUG] {src_name} ref_ee_xyz={self.ref_ee_xyz[src_name]}")
                    print(f"[DEBUG] {src_name} ref_ee_quat={self.ref_ee_quat[src_name]}")
                    print(f"[DEBUG] {src_name} q_before_solve={self.placo_robot.state.q[12:19].copy() if 'left' in src_name else self.placo_robot.state.q[19:26].copy()}")

                    # print("self.ref_ee_xyz[src_name]-----begin",src_name,self.ref_ee_xyz[src_name])
                xr_pose = self.xr_client.get_pose_by_name(config["pose_source"])
                # print("Current XR pose for {}: {}".format(src_name, xr_pose))
                delta_xyz, delta_rot = self._process_xr_pose(xr_pose, src_name)
                # [DEBUG] 打印 delta 和 IK 目标
                if self.ref_ee_xyz[src_name] is not None and np.linalg.norm(delta_xyz) > 0.001:
                    print(f"[DEBUG] {src_name} delta_xyz={delta_xyz}, delta_rot={delta_rot}")
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
                    # [DEBUG] 检查目标四元数是否有效
                    q_norm = np.linalg.norm(target_quat)
                    if not np.isfinite(q_norm) or abs(q_norm - 1.0) > 0.01:
                        print(f"[DEBUG] {src_name} INVALID target_quat={target_quat}, norm={q_norm}")
                    target_pose = tf.quaternion_matrix(target_quat)
                    target_pose[:3, 3] = target_xyz
                    self.effector_task[src_name].T_world_frame = target_pose


                # 放在这里是因为，一旦开始遥操之后尽量不能停，数采停止控制的时候，机械臂就保持原位
                # print("left_origin",(self.placo_robot.state.q[12:19]).tolist())
                # print("right_origin",(self.placo_robot.state.q[19:26]).tolist())
                #减去solver后与init的初始偏差
                l_ready_send = self.placo_robot.state.q[12:19].copy() - self.left_q_delta_init_and_solver
                r_ready_send = self.placo_robot.state.q[19:26].copy() -self.right_q_delta_init_and_solver
                l_real,r_real = self.real_arm_command_sender.angle_map(l_ready_send.tolist(), r_ready_send.tolist())
                # print("left_final",l_real)
                # print("right_final",r_real)
                
                right_trigger = self.xr_client.get_key_value_by_name('right_trigger')
                left_trigger  = self.xr_client.get_key_value_by_name('left_trigger')
                
                both_arm_grip = l_real.tolist() + r_real.tolist()
                
                both_arm_grip.append(left_trigger*100)  # 左臂夹爪位置
                both_arm_grip.append(right_trigger*100)  # 右臂夹爪位置


            else:
                if self.ref_ee_xyz[src_name] is not None:
                    print(f"{src_name} is deactivated.")
                    self.ref_ee_xyz[src_name] = None
                    self.ref_controller_xyz[src_name] = None
                    # 同步末端任务到当前位姿，防止 solver 继续漂移
                    ee_xyz, ee_quat = self._get_link_pose(config["link_name"])
                    if self.effector_control_mode[src_name] == "position":
                        self.effector_task[src_name].target_world = ee_xyz
                    else:
                        ee_target = tf.quaternion_matrix(ee_quat)
                        ee_target[:3, 3] = ee_xyz
                        self.effector_task[src_name].T_world_frame = ee_target
                    print("="*100)

                # 每帧更新 regularization 目标到当前值，冻结手臂
                if hasattr(self, 'joints_regularization_task'):
                        self.joints_regularization_task.set_joints(self._get_joint_values_dict())

            '''
            else:
                # pass
                if self.ref_ee_xyz[src_name] is not None:
                    print(f"{src_name} is deactivated.")
                    self.ref_ee_xyz[src_name] = None
                    self.ref_controller_xyz[src_name] = None
                    print("="*100)
            '''
        # Process motion tracker data
        self._update_motion_tracker_tasks()

        # [DEBUG] solver 前关节角度
        q_before = self.placo_robot.state.q[12:19].copy()
        try:
            self.solver.solve(True)
        except RuntimeError as e:
            print(f"IK solver failed: {e}")
        # [DEBUG] solver 后关节角度，检测跳变
        q_after = self.placo_robot.state.q[12:19].copy()
        q_diff = np.abs(q_after - q_before)
        if np.max(q_diff) > 0.1:
            print(f"[DEBUG] JUMP! q_before={np.round(q_before,4)}")
            print(f"[DEBUG] JUMP! q_after ={np.round(q_after,4)}")
            print(f"[DEBUG] JUMP diff  ={np.round(q_diff,4)}")


    def quat_to_euler_deg(self,quat_wxyz, axes='sxyz'):
        """
        将四元数 [w, x, y, z] 转换为欧拉角（单位：度），默认固定轴 XYZ。
        
        参数:
            quat_wxyz (list/tuple/np.ndarray): 四元数 [w, x, y, z]
            axes (str): 旋转顺序，默认 'sxyz'（静态坐标系 X→Y→Z）
        
        返回:
            list: [roll_deg, pitch_deg, yaw_deg]（单位：度）
        """
        q_xyzw = [quat_wxyz[1], quat_wxyz[2], quat_wxyz[3], quat_wxyz[0]]
        euler_rad = tf.euler_from_quaternion(q_xyzw, axes=axes)
        return np.rad2deg(euler_rad).tolist()  # 转为度并返回列表

    def euler_deg_to_quat(self,euler_deg, axes='sxyz'):
        """
        将欧拉角（单位：度）转换为四元数 [w, x, y, z]，旋转顺序需与上面一致。
        
        参数:
            euler_deg (list/tuple/np.ndarray): [roll_deg, pitch_deg, yaw_deg]（单位：度）
            axes (str): 旋转顺序，默认 'sxyz'
        
        返回:
            list: [w, x, y, z]
        """
        euler_rad = np.deg2rad(euler_deg)
        q_xyzw = tf.quaternion_from_euler(euler_rad[0], euler_rad[1], euler_rad[2], axes=axes)
        return [q_xyzw[3], q_xyzw[0], q_xyzw[1], q_xyzw[2]]

    def _update_ik(self):
        """
        This is the core IK logic block. It reads from XR, updates Placo tasks,
        and solves the kinematics.
        """
        if self.tcp_replay_mode:
            return
        self._handle_local_frame_recenter()
        if self._handle_xy_home_reset():
            return
        if self._handle_xr_connection():
            return



        self._update_robot_state()
        self.placo_robot.update_kinematics()
       
        for src_name, config in self.manipulator_config.items():
            xr_grip_val = self.xr_client.get_key_value_by_name(config["control_trigger"])
            # src_name 为 left_hand 或 right_hand 
            # xr_grip_val 为 0-1 之间的浮点数，0 表示关闭，1 表示打开
            # print(f"Grip value for {src_name}: {xr_grip_val}")
            self.active[src_name] = xr_grip_val > 0.9
            faults = getattr(self, '_precision_faults', set())
            if not self.active[src_name]:
                faults.discard(src_name)
            elif src_name in faults:
                self.active[src_name] = False
            # print(f"{src_name} active: {self.active[src_name]}")


            if self.active[src_name]:
                if self.ref_ee_xyz[src_name] is None :
                    print(f"{src_name} is activated.")
                    self.ref_ee_xyz[src_name], self.ref_ee_quat[src_name] = self._get_link_pose(config["link_name"])

                    # print("self.ref_ee_xyz[src_name]-----begin",src_name,self.ref_ee_xyz[src_name])
                xr_pose = self.xr_client.get_pose_by_name(config["pose_source"])
                # [x,y,z,x,y,z,w]

                if src_name == "right_hand":
                    which_side = "right"

                if src_name == "left_hand":
                    which_side = "left"
                
                # 提取四元数,转成欧拉角
                quat_wxyz = [xr_pose[6], xr_pose[3], xr_pose[4], xr_pose[5]]
                # 转成欧拉角（度）
                euler_deg = self.quat_to_euler_deg(quat_wxyz)   # 默认 'sxyz'
                now_pos = [xr_pose[0],xr_pose[1],xr_pose[2],euler_deg[0],euler_deg[1],euler_deg[2]]
                self._update_latest_pico_tcp_data(now_pos,which_side,True)
                # print("Current XR pose for {}: {}".format(src_name, xr_pose))
                from pico_f1.precision import PrecisionInputError
                try:
                    delta_xyz, delta_rot = self._process_xr_pose(xr_pose, src_name)
                except PrecisionInputError:
                    self.active[src_name] = False
                    self.ref_ee_xyz[src_name] = None
                    self.ref_controller_xyz[src_name] = None
                    continue
                # print("delta_xyz", delta_xyz)
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
                    # print("target_xyz_origin\n",target_xyz)
                    
                    #限制球形工作空间
                    # 以大臂第一个关节末端为球心
                    r = 1.5
                    if src_name == "left_hand":
                        # 左臂：先修正 Y 轴偏移（减去 1.0m）
                        target_xyz[1] -= 1.17
                        # 再限制距离
                        dist = np.linalg.norm(target_xyz)
                        if dist > r:
                            target_xyz = target_xyz / dist * r
                        target_xyz[1] += 1.17
                    elif src_name == "right_hand":
                        target_xyz[1] += 0.17
                        # 右臂：直接限制距离
                        dist = np.linalg.norm(target_xyz)
                        if dist > r:
                            target_xyz = target_xyz / dist * r
                        target_xyz[1] -= 0.17
                    target_pose = tf.quaternion_matrix(target_quat)
                    target_pose[:3, 3] = target_xyz
                    # print("target_pos_after:\n",target_pose[:3, 3]) 


                    self.effector_task[src_name].T_world_frame = target_pose

                # 放在这里是因为，一旦开始遥操之后尽量不能停，数采停止控制的时候，机械臂就保持原位
                #减去solver后与init的初始偏差
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
                
                right_trigger = self.xr_client.get_key_value_by_name('right_trigger')
                left_trigger  = self.xr_client.get_key_value_by_name('left_trigger')


                # Stage once below, after both activation states are sampled.
                # Hardware transmission belongs to the approval clock only.

            else:
                with self.pico_tcp_data_lock:
                    if src_name == "right_hand":
                        self.latest_right_active = False        

                    if src_name == "left_hand":
                        self.latest_left_active = False
            

                if self.ref_ee_xyz[src_name] is not None:
                    print(f"{src_name} is deactivated.")
                    self.ref_ee_xyz[src_name] = None
                    self.ref_controller_xyz[src_name] = None
                    print("="*100)

                # 停止时一次性更新 regularization 目标，防止漂移
                '''
                if hasattr(self, 'joints_reg_task'):
                # if hasattr(self, 'joints_regularization_task'):
                    current_joints = {name: q for name, q in zip(self.placo_robot.joint_names(), self.placo_robot.state.q)}
                    self.joints_reg_task.set_joints(current_joints)

                '''
        enabled = [bool(self.active.get(side + '_hand', False))
                   for side in ('left', 'right')]
        if any(enabled):
            raw_arm = l_final.tolist() + r_final.tolist()
            both_arm_grip = self._stage_and_get_safe_arm_command(raw_arm, enabled)
            both_arm_grip.extend([100 - left_trigger*100, 100 - right_trigger*100])
            self._update_latest_both_arm_grip(both_arm_grip)
        else:
            with self._trajectory_lock:
                self._arm_clutch.release(enabled)
                self._raw_arm_enabled = enabled

        # 每帧更新 regularization，防止双臂同时激活时不更新导致拖拽
        if hasattr(self, 'joints_regularization_task'):
            self.joints_regularization_task.set_joints(self._get_joint_values_dict())
        # Process motion tracker data
        self._update_motion_tracker_tasks()

        try:
            self.solver.solve(True)
        except RuntimeError as e:
            print(f"IK solver failed: {e}")



    def _update_motion_tracker_tasks(self):
        """Process motion tracker data and update corresponding Placo tasks."""
        motion_tracker_data = self.xr_client.get_motion_tracker_data()

        for src_name, config in self.manipulator_config.items():
            # Skip if no motion tracker configured for this end effector
            if "motion_tracker" not in config:
                continue

            # Skip if main controller is not active
            if not self.active.get(src_name, False):
                # Reset motion tracker references when controller is inactive
                if src_name in self.ref_tracker_xyz:
                    del self.ref_tracker_xyz[src_name]
                    del self.ref_robot_xyz[src_name]
                continue

            tracker_config = config["motion_tracker"]
            serial = tracker_config["serial"]

            # Skip if this tracker is not available
            if serial not in motion_tracker_data:
                continue

            # Get motion tracker pose
            tracker_pose = motion_tracker_data[serial]["pose"]
            tracker_xyz = self.R_headset_world @ np.array(tracker_pose[:3])

            # Initialize reference positions on first detection
            if src_name not in self.ref_tracker_xyz:
                self.ref_tracker_xyz[src_name] = tracker_xyz.copy()
                # Get current robot end-effector position as baseline
                robot_xyz, _ = self._get_link_pose(config["motion_tracker"]["link_target"])
                self.ref_robot_xyz[src_name] = robot_xyz.copy()

                # 强制同步 task，by lxl 2024-06-17，避免第一次激活时的跳变
                # if src_name in self.motion_tracker_task:
                #     self.motion_tracker_task[src_name].target_world = robot_xyz.copy()
                continue

            # Calculate movement delta from tracker's initial position
            tracker_delta = tracker_xyz - self.ref_tracker_xyz[src_name]

            # Apply scaled tracker movement to robot's initial position
            final_target_xyz = self.ref_robot_xyz[src_name] + tracker_delta * self.scale_factor

            # Update motion tracker task target position
            if src_name in self.motion_tracker_task:
                self.motion_tracker_task[src_name].target_world = final_target_xyz

    def _init_placo_viz(self):
        print("[VIZ] 正在初始化 Placo 可视化...")
        self.placo_vis = robot_viz(self.placo_robot)
        meshcat_url = self.placo_vis.viewer.url()
        print(f"[VIZ] Meshcat URL: {meshcat_url}")
        webbrowser.open(meshcat_url)
        self.placo_vis.display(self.placo_robot.state.q)
        print("[VIZ] 可视化初始化完成")
        for name, config in self.manipulator_config.items():
            robot_frame_viz(self.placo_robot, config["link_name"])
            
            # Show appropriate visualization based on control mode
            if self.effector_control_mode[name] == "position":
                # Create a frame matrix for position-only visualization
                target_frame = np.eye(4)
                target_frame[:3, 3] = self.effector_task[name].target_world
                frame_viz(f"vis_target_{name}", target_frame)
            else:
                # Full pose visualization
                frame_viz(f"vis_target_{name}", self.effector_task[name].T_world_frame)

            # Visualize motion tracker target if configured
            if "motion_tracker" in config and name in self.motion_tracker_task:
                link_target = config["motion_tracker"]["link_target"]
                robot_frame_viz(self.placo_robot, link_target)
                # Create a frame matrix for visualization
                tracker_frame = np.eye(4)
                tracker_frame[:3, 3] = self.motion_tracker_task[name].target_world
                frame_viz(f"vis_tracker_{name}", tracker_frame)

    def _update_placo_viz(self):
        # 更新可视化显示当前机器人状态 0-20,6-20是双臂  
        self.placo_vis.display(self.placo_robot.state.q)
        self.right_eef = self.xr_client.get_key_value_by_name('right_trigger')
        self.left_eef = self.xr_client.get_key_value_by_name('left_trigger')
        # ==================== 删除：_collect_data_if_active 调用 ====================
        # self._collect_data_if_active()  # 新增：每帧采集数据（如果正在采集）
        # 功能由 ROS2 pub 线程替代
        # ==================== 删除结束 ====================
        # print("Updated Placo visualization with current robot state.\n",self.placo_robot.state.q)
        for name, config in self.manipulator_config.items():
            robot_frame_viz(self.placo_robot, config["link_name"])
            # Show appropriate visualization based on control mode
            if self.effector_control_mode[name] == "position":
                # Create a frame matrix for position-only visualization
                target_frame = np.eye(4)
                target_frame[:3, 3] = self.effector_task[name].target_world
                frame_viz(f"vis_target_{name}", target_frame)
            else:
                # Full pose visualization
                frame_viz(f"vis_target_{name}", self.effector_task[name].T_world_frame)

            # Update motion tracker target visualization if configured
            if "motion_tracker" in config and name in self.motion_tracker_task:
                link_target = config["motion_tracker"]["link_target"]
                robot_frame_viz(self.placo_robot, link_target)
                # Create a frame matrix for visualization
                tracker_frame = np.eye(4)
                tracker_frame[:3, 3] = self.motion_tracker_task[name].target_world
                frame_viz(f"vis_tracker_{name}", tracker_frame)

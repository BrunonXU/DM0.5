import abc
import threading
from typing import Any, Dict

import meshcat.transformations as tf
import numpy as np
import placo

from xrobotoolkit_teleop.common.data_logger import DataLogger
from xrobotoolkit_teleop.common.xr_client import XrClient
from xrobotoolkit_teleop.utils.geometry import (
    apply_delta_pose,
    quat_diff_as_angle_axis, 
)
from xrobotoolkit_teleop.utils.parallel_gripper_utils import (
    calc_parallel_gripper_position,
)

from . import send2real_robot_f1_udp as send2real_robot_f1
from .base_teleop_controller_ros2_mixin import Ros2TeleopMixin
from .base_teleop_controller_ik_mixin import IkVizMixin


import time  # 新增：用于时间戳和 sleep
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(os.environ["PICO_F1_ROOT"]).resolve()
module_dir = str(PROJECT_ROOT / "vendor" / "f1_safety" / "src" / "python")
config_dir = str(PROJECT_ROOT / "config")
for local_path in (module_dir, config_dir):
    if local_path not in sys.path:
        sys.path.insert(0, local_path)
import ip_config
from pico_f1.config import load_control_config
from pico_f1.chassis import (
    ABButtonArbiter,
    BodyVelocityFollower,
    JoystickChassisController,
    pose_in_headset_heading_frame,
)
from pico_f1.buttons import ReviewButtonArbiter, XYRecordingArbiter
from pico_f1.session_pose import ARM_JOINT_NAMES, load_session_pose
from pico_f1.torso import TorsoPitchMapper
from pico_f1.trajectory import JerkLimitedTrajectory

# ==================== 新增：ROS2 导入 ====================
import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import JointState
from std_msgs.msg import Bool, String
from control_msgs.msg import GripperCommand
from geometry_msgs.msg import TwistStamped
from move.msg import State as MoveState
# ==================== 新增结束 ====================




# 导入渊博安全监测算法模块
# 先导入依赖模块，将其注册到 sys.modules，这样主模块内部的 import 会直接从缓存获取
try:
    import F1_safe_control_real_with_com
except ImportError as e:
    # 如果导入失败，可能是该模块本身还有问题，但我们仍可尝试继续
    # 可以打印警告，但为了健壮性，使用 importlib 再次尝试加载
    import importlib.util
    dep_path = os.path.join(module_dir, 'F1_safe_control_real_with_com.py')
    if os.path.isfile(dep_path):
        spec = importlib.util.spec_from_file_location("F1_safe_control_real_with_com", dep_path)
        if spec:
            dep_mod = importlib.util.module_from_spec(spec)
            sys.modules["F1_safe_control_real_with_com"] = dep_mod
            spec.loader.exec_module(dep_mod)
        else:
            raise
    else:
        raise




# 现在主模块可以正常导入
from com_and_collision_protect_modules import SafetyProtector, load_safety_config
from _safety_protector import ALL_JOINT_NAMES



class BaseTeleopController(IkVizMixin, Ros2TeleopMixin, abc.ABC):
    def __init__(
        self,
        robot_urdf_path: str,
        manipulator_config: Dict[str, Dict[str, Any]],
        floating_base: bool,
        R_headset_world: np.ndarray,
        scale_factor: float,
        q_init: np.ndarray,
        dt: float,
        enable_log_data: bool = False,
        log_dir: str = "logs",
        log_freq: float = 50,
    ):
        self.robot_urdf_path = robot_urdf_path
        self.manipulator_config = manipulator_config
        self.floating_base = floating_base
        self.R_headset_world = R_headset_world
        self.scale_factor = scale_factor
        self._control_config = load_control_config()
        self._chassis_config = self._control_config["chassis"]
        self._torso_config = self._control_config["torso_pitch"]
        self._recording_config = self._control_config["recording"]
        control_dt = 1.0 / float(self._control_config["command_hz"])
        self._chassis_follow = BodyVelocityFollower(
            self._chassis_config,
            dt=control_dt,
        )
        self._joystick_chassis = JoystickChassisController(
            self._chassis_config,
            dt=control_dt,
        )
        self._ab_arbiter = ABButtonArbiter(
            float(self._chassis_config["ab_chord_window_s"]),
            float(self._torso_config["a_hold_s"]),
        )
        self._torso_mapper = TorsoPitchMapper(self._torso_config)
        self._chassis_lock = threading.Lock()
        self._chassis_planner_lock = threading.RLock()
        self._chassis_enabled = False
        self._torso_enabled = False
        self._chassis_last_body_timestamp = None
        self._chassis_last_body_fresh_monotonic = 0.0
        self._chassis_last_head_timestamp = None
        self._chassis_last_head_fresh_monotonic = 0.0
        self._chassis_active_source = None
        self._chassis_command_mode = "idle"
        self._chassis_fault_reported = False
        self._joystick_last_timestamp = None
        self._joystick_last_fresh_monotonic = 0.0
        self._torso_local_frame_generation_seen = 0
        self._torso_fault_reported = False
        self._torso_last_timestamp = None
        self._torso_last_fresh_monotonic = 0.0
        self._local_frame_lock = threading.Lock()
        self._local_frame_generation = 0
        self._arm_local_frame_generation_seen = 0
        self._chassis_local_frame_generation_seen = 0
        self._local_frame_button_pressed = False
        self._local_frame_button_fault_reported = False
        self._chassis_feedback_lock = threading.Lock()
        self._chassis_feedback_speed = [0.0, 0.0, 0.0]
        self._chassis_feedback_pose = [0.0, 0.0, 0.0]
        self._chassis_feedback_monotonic = 0.0
        self._session_pose = load_session_pose()
        session_joints = self._session_pose["joints"]
        session_arms = [session_joints[name] for name in ARM_JOINT_NAMES]
        self.left_q_origin = session_arms[:7]
        self.right_q_origin = session_arms[7:]
        self.left_q_reset = list(self.left_q_origin)
        self.right_q_reset = list(self.right_q_origin)
        print(
            f"SESSION_INITIAL_POSE_LOADED joints={len(self._session_pose['name'])} "
            f"path={self._session_pose['path']}"
        )
        self.q_init = q_init
        self.dt = dt
        self.xr_client = XrClient()

        self.enable_log_data = enable_log_data
        self.log_dir = log_dir
        self.log_freq = log_freq
        if enable_log_data:
            self.data_logger = DataLogger(log_dir=log_dir)

        # Initial poses
        self.ref_ee_xyz = {name: None for name in manipulator_config.keys()}
        self.ref_ee_quat = {name: None for name in manipulator_config.keys()}
        self.ref_controller_xyz = {name: None for name in manipulator_config.keys()}
        self.ref_controller_quat = {name: None for name in manipulator_config.keys()}
        self.effector_task = {}
        self.effector_control_mode = {}  # Store control mode for each end effector
        self.active = {}
        self.gripper_pos_target = {}

        # Motion tracker support
        self.motion_tracker_task = {}
        self.ref_tracker_xyz = {}  # Store initial tracker positions
        self.ref_robot_xyz = {}  # Store initial robot end-effector positions
        for name, config in self.manipulator_config.items():
            if "gripper_config" in config:
                gripper_config = config["gripper_config"]
                self.gripper_pos_target[name] = {
                    joint_name: joint_pos
                    for joint_name, joint_pos in zip(gripper_config["joint_names"], gripper_config["open_pos"])
                }

        self._stop_event = threading.Event()
        self._trajectory_lock = threading.RLock()
        self._reset_in_progress = threading.Event()
        self._reset_request = threading.Event()

        self._robot_setup()
        self._placo_setup()

        print("Placo joint names and ACTUAL q indices:")
        for joint_id in range(self.placo_robot.model.njoints):
            name = self.placo_robot.model.names[joint_id]
            q_idx = self.placo_robot.model.idx_qs[joint_id]
            print(f"  joint_id={joint_id}, q[{q_idx}] → {name}")


        # 新增：数据采集相关
        # self.data_collector = save_data_lxl_4.FollowJointsRecorder()  # 预创建一个实例，实际文件名会在开始采集时更新
        # self.collector = save_data_lxl_4.FollowJointsRecorder()
        # self.collector = None  # McapDataCollector 实例，延迟创建
        self.is_collecting = False  # 是否正在采集
        self.collect_thread = None  # 采集线程
        self._recording_state = "idle"
        self._xy_recording_arbiter = XYRecordingArbiter(
            float(self._recording_config["double_click_s"]),
            float(self._recording_config["button_debounce_s"]),
        )
        self._review_button_arbiter = ReviewButtonArbiter(
            float(self._recording_config["double_click_s"]),
            float(self._recording_config["button_debounce_s"]),
        )

        # self.realsense_recorder = save_realsense_data3.RealsenseVideoRecorder()  # 初始化 Realsense 录制器

        self.filename_created = False  # 新增：标志位，控制是否创建新的 MCAP 文件

        self.real_arm_command_sender = send2real_robot_f1.RealArmCommandSender()

        # Start button polling only after every object it can call exists.
        self._start_collection_thread()


        self.first_get_ref_ee_xyz_flag = False

        self.sync_placo_to_real(False)
        # time.sleep(2)

        print("初始关节角度\n")
        print("left_arm_state\n",self.placo_robot.state.q[12:19].copy())
        print("right_arm_state\n",self.placo_robot.state.q[19:26].copy())

        # 因为初始状态仿真中跟实际是一样的状态   这里获取的是弧度
        self.left_q_real_and_sim_init = self.placo_robot.state.q[12:19].copy()
        self.right_q_real_and_sim_init = self.placo_robot.state.q[19:26].copy()


        self._update_robot_state()
        self.placo_robot.update_kinematics()


   
        #这里求解更新后，仿真中关节角度会发生变化，但位姿不变
        self.solver.solve(True)
        print("更新运动学后关节角度\n")
        print("left_arm_state\n",self.placo_robot.state.q[12:19].copy())
        print("right_arm_state\n",self.placo_robot.state.q[19:26].copy())


        self.left_q_after_solver = self.placo_robot.state.q[12:19].copy()
        self.right_q_after_solver = self.placo_robot.state.q[19:26].copy() 

        #计算当前仿真与初始状态关节角度的偏差  
        #  self.placo_robot.state.q[12:19] 类型 <class 'numpy.ndarray'>
        #记录下这个初始偏差
        self.left_q_delta_init_and_solver = self.left_q_after_solver - self.left_q_real_and_sim_init
        self.right_q_delta_init_and_solver = self.right_q_after_solver - self.right_q_real_and_sim_init
        print("偏差\n",self.left_q_delta_init_and_solver,self.right_q_delta_init_and_solver)

        for i in range(5):
            self._update_ik_lxl()
            time.sleep(0.2)
        print("updata_ik_lxl 5 later")

        self.left_q_after_solver = self.placo_robot.state.q[12:19].copy()
        self.right_q_after_solver = self.placo_robot.state.q[19:26].copy()
        print("left_arm_state\n", self.left_q_after_solver )
        print("right_arm_state\n",self.right_q_after_solver)
        self.left_q_delta_init_and_solver = self.left_q_after_solver - self.left_q_real_and_sim_init
        self.right_q_delta_init_and_solver = self.right_q_after_solver - self.right_q_real_and_sim_init
        print("final偏差\n",self.left_q_delta_init_and_solver,self.right_q_delta_init_and_solver)

        # flag 标志位
        self.xy_pressed_flag_once = False
        self.origin_begin_flag_once = True
        self.reset_already = False
        self.origin_begin_flag = False

        #liang zhi bi dan du kong zhi
        self.right_control_flag = False
        self.left_control_flag = False


        # 新增pico末端位姿数据发布 =========================
        self.pico_tcp_pos_names = ['x','y','z','rx','ry','rz']
        self.latest_left_tcp_pos = [6]*6
        self.latest_right_tcp_pos = [6]*6
        self.pico_tcp_data_lock = threading.Lock()
        self.pico_left_tcp_data_updated = False
        self.pico_right_tcp_data_updated = False

        self.latest_left_active = False
        self.latest_right_active = False

        

        # 在 __init__ 中添加（放在所有订阅创建之后）
        self._ros_spin_thread = threading.Thread(target=self._ros_spin, daemon=True)
        self._ros_spin_thread.start()

        self.tcp_replay_mode = False


        # ==================== 新增：ROS2 发布器初始化 ====================
        # both_arm_grip 数据结构: [LJ1..LJ7, RJ1..RJ7, left_gripper, right_gripper] = 16 个值
        #self._pub_lead_joint_names = [
            #"left_joint1", "left_joint2", "left_joint3", "left_joint4",
            #"left_joint5", "left_joint6", "left_joint7", 
            #"right_joint1", "right_joint2", "right_joint3", "right_joint4",
            #"right_joint5", "right_joint6", "right_joint7",
        #]
 
        self.left_joint_names = ['arm_L_joint1', 'arm_L_joint2', 'arm_L_joint3', 'arm_L_joint4',
                                 'arm_L_joint5', 'arm_L_joint6', 'arm_L_joint7']
        self.right_joint_names = ['arm_R_joint1', 'arm_R_joint2', 'arm_R_joint3', 'arm_R_joint4',
                                  'arm_R_joint5', 'arm_R_joint6', 'arm_R_joint7']
        self._pub_lead_joint_names = self.left_joint_names + self.right_joint_names        

        # 线程安全的数据缓存
        self._latest_both_arm_grip = [0.0] * 16
        self._both_arm_grip_lock = threading.Lock()
        self._both_arm_grip_updated = False  # 标记是否有新数据
        
        # 初始化 ROS2
        try:
            if not rclpy.ok():
                rclpy.init(args=None)
        except RuntimeError as e:
            if "Context.init() must only be called once" in str(e):
                print("[WARN] ROS2 上下文已存在，尝试关闭后重新初始化...")
                rclpy.shutdown()
                time.sleep(0.5)  # 等待清理
                rclpy.init(args=None)
            else:
                raise
        self._ros_node = rclpy.create_node('teleop_joint_state_publisher')
        self._joint_state_pub = self._ros_node.create_publisher(
            JointState, '/lead/joint_states', 10
        )
        self.pico_left_tcp_pub = self._ros_node.create_publisher(
            JointState, '/lead/left_tcp', 10
        )
        self.pico_right_tcp_pub = self._ros_node.create_publisher(
            JointState, '/lead/right_tcp', 10
        )
        self._gripper_left_pub = self._ros_node.create_publisher(
            GripperCommand, '/motion_ctl/gripper/left', 10
        )
        self._gripper_right_pub = self._ros_node.create_publisher(
            GripperCommand, '/motion_ctl/gripper/right', 10
        )
        # Velocity commands are latest-state, not a FIFO of past motion.
        chassis_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                                 history=HistoryPolicy.KEEP_LAST)
        self._chassis_cmd_pub = self._ros_node.create_publisher(
            TwistStamped, self._chassis_config["command_topic"], chassis_qos
        )
        self._chassis_diagnostics_pub = self._ros_node.create_publisher(
            String, '/pico/chassis_diagnostics', 1
        )
        self._chassis_enabled_pub = self._ros_node.create_publisher(
            Bool, '/pico/chassis_enabled', 10
        )
        torso_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST,
        )
        self._torso_joint_pub = self._ros_node.create_publisher(
            JointState, self._torso_config["command_topic"], torso_qos
        )
        self._chassis_feedback_sub = self._ros_node.create_subscription(
            MoveState,
            self._chassis_config["feedback_topic"],
            self._chassis_feedback_callback,
            chassis_qos,
        )
        recording_status_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self._recording_status_sub = self._ros_node.create_subscription(
            String,
            "/pico/recording_status",
            self._recording_status_callback,
            recording_status_qos,
        )
        #新增节后主臂pico数据，并进行遥操控制接口，ros2 topic
        # 订阅目标位姿 topic（与发布的 topic 一致）
        self._left_pose_sub = self._ros_node.create_subscription(
            JointState,
            '/lead/left_tcp_control',
            # '/lead/left_tcp',
            self._external_left_pose_callback,
            10
        )
        self._right_pose_sub = self._ros_node.create_subscription(
            JointState,
            '/lead/right_tcp_control',
            # '/lead/right_tcp',
            self._external_right_pose_callback,
            10
        )

        self._replay_mode_sub = self._ros_node.create_subscription(
            Bool,
            '/lead_tcp/replay_mode',          
            self._replay_mode_callback,
            10
        )




        self.hal_full_body_joint_states = np.zeros(34)
        self._hal_joint_position_by_name = {}
        self._hal_joint_states_lock = threading.Lock()
        self._hal_joint_states_received = False
        self._hal_joint_states_last_monotonic = 0.0
        self._hal_joint_states_sub = self._ros_node.create_subscription(
            JointState,
            '/hal/joint_states',
            self._hal_joint_states_callback,
            10
        )
        # 等待首次数据（最多 5 秒），超时则保持全 0
        _hal_wait_start = time.time()
        while not self._hal_joint_states_received and (time.time() - _hal_wait_start) < 5.0:
            time.sleep(0.1)

        if not self._hal_joint_states_received:
            raise RuntimeError("[FATAL] 未收到 /hal/joint_states，全身关节状态缺失，程序终止。")
        else:
            print("[INFO] 已成功获取 /hal/joint_states 全身关节状态")

        # ==================== 新增结束 ====================

        # ==================== 碰撞检测初始化（最终修复版） ====================
        self._safety = SafetyProtector(use_com_guard=False)
        self._safety.init_safe_cmd(
            np.deg2rad(self.hal_full_body_joint_states[:24])
        )

        self.collision_check_freq = float(
            self._control_config["collision_check_hz"]
        )
        self._feedback_timeout_s = float(
            self._control_config["feedback_timeout_s"]
        )
        self._safety_thread_stop = threading.Event()
        self._safety_lock = threading.Lock()

        # Until the first desired command passes CollisionGuard, publishing
        # holds the measured physical posture rather than an unchecked target.
        initial_arm_joints = self.hal_full_body_joint_states[:14].copy()
        self._last_safe_left = initial_arm_joints[0:7]
        self._last_safe_right = initial_arm_joints[7:14]
        self._trajectory = JerkLimitedTrajectory(
            size=14,
            dt=1.0 / self.collision_check_freq,
            max_velocity=float(self._control_config["max_velocity_deg_s"]),
            max_acceleration=float(
                self._control_config["max_acceleration_deg_s2"]
            ),
            max_jerk=float(self._control_config["max_jerk_deg_s3"]),
            target_cutoff_hz=float(self._control_config["target_cutoff_hz"]),
            deadband=float(self._control_config["joint_deadband_deg"]),
        )
        self._trajectory.reset(initial_arm_joints)
        self._last_raw_arm_joints = initial_arm_joints.copy()
        self._raw_jump_events = 0
        torso_index = int(self._torso_config["joint_index"])
        initial_torso_pitch = float(self.hal_full_body_joint_states[torso_index])
        self._torso_trajectory = JerkLimitedTrajectory(
            size=1,
            dt=1.0 / self.collision_check_freq,
            max_velocity=float(self._torso_config["max_velocity_deg_s"]),
            max_acceleration=float(self._torso_config["max_acceleration_deg_s2"]),
            max_jerk=float(self._torso_config["max_jerk_deg_s3"]),
            target_cutoff_hz=float(self._torso_config["target_cutoff_hz"]),
            deadband=float(self._torso_config["joint_deadband_deg"]),
        )
        self._torso_trajectory.reset([initial_torso_pitch])
        self._torso_output_lock = threading.Lock()
        self._torso_output_trajectory = JerkLimitedTrajectory(
            size=1,
            dt=1.0 / float(self._torso_config["output_hz"]),
            max_velocity=float(self._torso_config["max_velocity_deg_s"]),
            max_acceleration=float(self._torso_config["max_acceleration_deg_s2"]),
            max_jerk=float(self._torso_config["max_jerk_deg_s3"]),
            target_cutoff_hz=float(
                self._torso_config["output_target_cutoff_hz"]
            ),
            deadband=float(self._torso_config["output_deadband_deg"]),
        )
        self._torso_output_trajectory.reset([initial_torso_pitch])
        self._torso_output_target = initial_torso_pitch
        self._torso_output_active = False
        self._torso_output_stop = threading.Event()
        self._raw_torso_pitch = None
        self._last_safe_torso_pitch = initial_torso_pitch
        self._torso_command_active = False
        
        self._freeze_left_arm = False
        self._freeze_right_arm = False
        self._raw_arm_joints = None   # 用于碰撞检测的原始期望位置

        self._start_collision_detection_thread()
        self._torso_output_thread = threading.Thread(
            target=self._torso_output_loop,
            daemon=True,
            name="TorsoOutput1000Hz",
        )
        self._torso_output_thread.start()
        print(
            f"[TORSO] approved waypoint resampler started at "
            f"{float(self._torso_config['output_hz']):g}Hz"
        )
        # ==================== 初始化结束 ====================

        self._pub_thread_stop = threading.Event()
        self._chassis_worker_stop = threading.Event()
        self._chassis_worker_thread = threading.Thread(
            target=self._chassis_publish_loop, daemon=True, name="ChassisOutput50Hz"
        )
        self._chassis_worker_thread.start()
        self._pub_thread = threading.Thread(target=self._publish_loop, daemon=True)
        self._pub_thread.start()
        print(
            f"[ROS2] approved-command publisher started at "
            f"{self._control_config['command_hz']}Hz"
        )

    def _start_collision_detection_thread(self):
        """启动独立碰撞检测线程"""
        def collision_detection_loop():
            period = 1.0 / self.collision_check_freq
            count = 0
            last_print_time = time.time()
            start_time = time.time()
            
            while not self._safety_thread_stop.is_set():
                loop_start = time.time()
                
                try:
                    self._perform_collision_check(count)
                except Exception as exc:
                    # Last-resort containment: the sole safety worker must
                    # never disappear silently. Hold outputs and keep the
                    # thread alive so fresh feedback can recover it.
                    with self._safety_lock:
                        self._freeze_left_arm = True
                        self._freeze_right_arm = True
                        self._trajectory.hold()
                        self._torso_trajectory.hold()
                    print(f"[SAFETY WORKER RECOVERY] held one cycle: {exc}")
                
                count += 1
                elapsed = time.time() - loop_start
                sleep_time = max(0.0, period - elapsed)
                time.sleep(sleep_time)
                
                # 每100次检测打印一次真实频率（约每20秒打印一次，如果是5Hz）
                if count % 100 == 0:
                    current_time = time.time()
                    duration = current_time - last_print_time
                    real_freq = 100 / duration if duration > 0 else 0
                    avg_freq = count / (current_time - start_time)
                    
                    # print(f"[Safety Thread] 真实频率 ≈ {real_freq:.2f} Hz | 平均频率 ≈ {avg_freq:.2f} Hz | 循环次数: {count}")
                    last_print_time = current_time

        self._collision_thread = threading.Thread(
            target=collision_detection_loop, 
            daemon=True,
            name="CollisionDetectionThread"
        )
        self._collision_thread.start()
        print(f"[Safety] 碰撞检测线程已启动，目标频率 {self.collision_check_freq}Hz")


    def _perform_collision_check(self, count=0):
        """执行一次碰撞检测"""
        if self._reset_in_progress.is_set():
            return
        # Reset/rebase and CollisionGuard must never mutate the Ruckig state
        # concurrently.  A check is normally below one 20 ms control period.
        with self._trajectory_lock:
            if self._reset_in_progress.is_set():
                return
            self._perform_collision_check_locked(count)

    def _perform_collision_check_locked(self, count=0):
        """Collision check with the trajectory/reset lock already held."""
        xr = getattr(self, 'xr_client', None)
        if xr is not None and hasattr(xr, 'input_ready') and not xr.input_ready():
            with self._safety_lock:
                self._freeze_left_arm = True
                self._freeze_right_arm = True
                self._trajectory.hold()
                self._torso_trajectory.hold()
                held = np.concatenate((self._last_safe_left, self._last_safe_right))
            with self._both_arm_grip_lock:
                self._raw_arm_joints = held.copy()
                self._raw_torso_pitch = None
                self._torso_command_active = False
            return
        with self._both_arm_grip_lock:
            requested_arm_joints = (
                None if self._raw_arm_joints is None else self._raw_arm_joints.copy()
            )
            requested_torso_pitch = self._raw_torso_pitch
            torso_active = bool(self._torso_command_active)
        if requested_arm_joints is None and (requested_torso_pitch is None or not torso_active):
            return

        # Grip authorizes each arm independently. Placo solves both arms and
        # reset offset calibration can change the inactive solution too; neither
        # is permission to move that arm. Sample grips in this safety cycle,
        # not the sequential/stale IK self.active dictionary.
        arm_enabled = [True] * 14
        if xr is not None:
            arm_enabled = ([xr.get_key_value_by_name('left_grip') > .9] * 7
                           + [xr.get_key_value_by_name('right_grip') > .9] * 7)
        self._trajectory.hold_axes(arm_enabled)
        if requested_arm_joints is not None:
            requested_arm_joints = np.where(arm_enabled, requested_arm_joints,
                                             self._trajectory.position)

        with self._hal_joint_states_lock:
            target = self.hal_full_body_joint_states.copy()
            feedback_age = time.monotonic() - self._hal_joint_states_last_monotonic

        # Never evaluate or publish against stale physical state.  This can
        # happen when the motor process, ROS graph, or joint-state stream dies.
        if feedback_age > self._feedback_timeout_s:
            with self._safety_lock:
                self._freeze_left_arm = True
                self._freeze_right_arm = True
                self._trajectory.hold()
                self._torso_trajectory.hold()
            if count % 30 == 0:
                print(
                    f"[SAFETY WARN] /hal/joint_states stale "
                    f"({feedback_age:.3f}s); freezing both arms"
                )
            return
        try:
            arm_proposal = None
            if requested_arm_joints is not None and len(requested_arm_joints) >= 14:
                arm_proposal = self._trajectory.propose(requested_arm_joints)
                # Exact command invariant even if the numerical planner emits
                # tiny residual changes on stationary axes.
                from dataclasses import replace
                arm_proposal = replace(arm_proposal,
                    position=tuple(q if on else self._trajectory.position[i]
                                   for i, (q, on) in enumerate(zip(arm_proposal.position, arm_enabled))),
                    velocity=tuple(v if on else 0. for v, on in zip(arm_proposal.velocity, arm_enabled)),
                    acceleration=tuple(a if on else 0. for a, on in zip(arm_proposal.acceleration, arm_enabled)),
                    filtered_target=tuple(q if on else self._trajectory.position[i]
                                   for i, (q, on) in enumerate(zip(arm_proposal.filtered_target, arm_enabled))))
            torso_proposal = None
            if requested_torso_pitch is not None and torso_active:
                torso_proposal = self._torso_trajectory.propose([requested_torso_pitch])
        except Exception as exc:
            # Never let an online planner exception kill the sole collision
            # worker. Rebase at live feedback, hold this cycle, and allow the
            # following cycle to ramp toward the still-current XR target.
            torso_index = int(self._torso_config["joint_index"])
            measured_arms = np.where(arm_enabled, np.asarray(target[:14], dtype=float),
                                     self._trajectory.position)
            measured_torso = float(target[torso_index])
            with self._safety_lock:
                self._last_safe_left = measured_arms[:7].copy()
                self._last_safe_right = measured_arms[7:].copy()
                self._last_safe_torso_pitch = measured_torso
                self._freeze_left_arm = True
                self._freeze_right_arm = True
                self._trajectory.reset(measured_arms)
                self._torso_trajectory.reset([measured_torso])
            print(
                "[TRAJECTORY RECOVERY] planner rejected dynamic state; "
                f"rebased to live feedback and held one cycle: {exc}"
            )
            return
        if arm_proposal is not None:
            target[:14] = np.asarray(arm_proposal.position, dtype=float)
        if torso_proposal is not None:
            target[int(self._torso_config["joint_index"])] = float(
                torso_proposal.position[0]
            )

        try:
            is_safe, unsafe_index_joint, _, _ = self._safety.run(np.deg2rad(target[:24]))
            
            with self._safety_lock:
                self._freeze_left_arm = False
                self._freeze_right_arm = False

                if not is_safe:
                    left_unsafe = any(0 <= idx <= 6 for idx in unsafe_index_joint)
                    right_unsafe = any(7 <= idx <= 13 for idx in unsafe_index_joint)

                    # An unsafe result without a specific arm index is not
                    # actionable, so fail closed for both arms.
                    if not left_unsafe and not right_unsafe:
                        left_unsafe = True
                        right_unsafe = True

                    self._freeze_left_arm = left_unsafe
                    self._freeze_right_arm = right_unsafe

                    if arm_proposal is not None:
                        arm_joints_for_check = np.asarray(
                            arm_proposal.position, dtype=float
                        )
                        # Advance only the arm that CollisionGuard did not mark.
                        if not left_unsafe:
                            self._last_safe_left = arm_joints_for_check[0:7].copy()
                        if not right_unsafe:
                            self._last_safe_right = arm_joints_for_check[7:14].copy()
                        accepted = [not left_unsafe] * 7 + [not right_unsafe] * 7
                        self._trajectory.commit(arm_proposal, accepted)
                    # A global unsafe result can be caused by the torso change;
                    # arm-only collision indices cannot prove that body_j3 is safe.
                    if torso_proposal is not None:
                        self._torso_trajectory.hold()

                    if count % 30 == 0:
                        print(
                            f"[SAFETY WARN] collision: left_frozen={left_unsafe} "
                            f"right_frozen={right_unsafe} torso_frozen={torso_proposal is not None}"
                        )
                else:
                    if arm_proposal is not None:
                        arm_joints_for_check = np.asarray(
                            arm_proposal.position, dtype=float
                        )
                        self._last_safe_left = arm_joints_for_check[0:7].copy()
                        self._last_safe_right = arm_joints_for_check[7:14].copy()
                        self._trajectory.commit(arm_proposal)
                    if torso_proposal is not None:
                        self._last_safe_torso_pitch = float(torso_proposal.position[0])
                        self._torso_trajectory.commit(torso_proposal)
                    if count % 100 == 0:
                        # print("[SAFETY] 两臂均安全")
                        pass
                if torso_proposal is not None:
                    self._publish_approved_torso_position(
                        self._last_safe_torso_pitch
                    )
                        
        except Exception as e:
            print(f"[Safety Error] 碰撞检测异常: {e}")
            with self._safety_lock:
                self._freeze_left_arm = True
                self._freeze_right_arm = True
                self._trajectory.hold()
                self._torso_trajectory.hold()

    def _stage_and_get_safe_arm_command(self, raw_arm):
        """Stage a 14-DoF target and return only the last safety-approved pose.

        The CollisionGuard runs asynchronously.  Callers must never send
        ``raw_arm`` directly because the safety thread may not have evaluated
        that target yet.
        """
        candidate = np.asarray(raw_arm, dtype=float)
        if candidate.shape != (14,) or not np.all(np.isfinite(candidate)):
            raise ValueError("arm command must contain 14 finite joint values")

        # This is an IK-target diagnostic threshold, not an actuator step
        # limit. A target jump must still be accepted by the causal filter;
        # otherwise the comparison baseline never advances and every later
        # frame is rejected forever. Ruckig bounds the approved actuator
        # trajectory, and CollisionGuard evaluates every proposed 20 ms step.
        max_step = float(self._control_config["max_raw_target_step_deg"])
        raw_step = float(np.max(np.abs(candidate - self._last_raw_arm_joints)))
        if raw_step > max_step:
            self._raw_jump_events += 1
            if self._raw_jump_events == 1 or self._raw_jump_events % 250 == 0:
                print(
                    f"[TRAJECTORY] conditioning raw IK target delta {raw_step:.3f} deg "
                    f"(diagnostic threshold {max_step:.3f} deg) through low-pass/Ruckig"
                )
        self._last_raw_arm_joints = candidate.copy()

        with self._both_arm_grip_lock:
            self._raw_arm_joints = candidate.copy()
        with self._safety_lock:
            safe = np.concatenate(
                (self._last_safe_left.copy(), self._last_safe_right.copy())
            )
        return safe.tolist()

    def _begin_motion_reset(self):
        """Pause trajectory evolution before the bridge performs X+Y home."""
        self._reset_in_progress.set()
        if hasattr(self, "_chassis_lock"):
            with self._chassis_lock:
                self._chassis_enabled = False
                self._torso_enabled = False
        with self._chassis_planner_lock:
            if hasattr(self, "_chassis_follow"):
                self._chassis_follow.emergency_stop()
            if hasattr(self, "_joystick_chassis"):
                self._joystick_chassis.emergency_stop()
        # Wait for any in-flight safety proposal to finish, then discard the
        # pre-reset raw target.  No UDP arm frame is emitted by this method.
        with self._trajectory_lock:
            with self._both_arm_grip_lock:
                self._raw_arm_joints = None
                self._raw_torso_pitch = None
                self._torso_command_active = False
            with self._safety_lock:
                self._trajectory.hold()
                self._torso_trajectory.hold()
            self._torso_mapper.deactivate()
        if hasattr(self, "_torso_output_lock"):
            with self._torso_output_lock:
                self._torso_output_active = False

    def _wait_for_arm_target(
        self, target, timeout_s=240.0, tolerance_deg=2.0, stable_samples=3
    ):
        """Return live arm feedback once X+Y home has actually settled."""
        expected = np.asarray(target, dtype=float)
        if expected.shape != (14,) or not np.all(np.isfinite(expected)):
            raise ValueError("reset target must contain 14 finite joint values")
        deadline = time.monotonic() + float(timeout_s)
        stable = 0
        latest = None
        max_error = float("inf")
        while time.monotonic() < deadline:
            with self._hal_joint_states_lock:
                latest = self.hal_full_body_joint_states[:14].copy()
                feedback_age = (
                    time.monotonic() - self._hal_joint_states_last_monotonic
                )
            if feedback_age <= self._feedback_timeout_s and np.all(np.isfinite(latest)):
                max_error = float(np.max(np.abs(latest - expected)))
                stable = stable + 1 if max_error <= tolerance_deg else 0
                if stable >= int(stable_samples):
                    return latest
            time.sleep(0.02)
        print(
            f"[RESET BLOCKED] feedback did not settle within {timeout_s:.1f}s "
            f"(max_error={max_error:.3f} deg); teleoperation remains paused"
        )
        return None

    def _wait_for_reset_target(
        self,
        arm_target,
        torso_target,
        timeout_s=60.0,
        arm_tolerance_deg=2.0,
        torso_tolerance_deg=0.3,
        stable_samples=3,
    ):
        """Wait until both arms and body_j3 have settled after X+Y reset."""
        expected_arms = np.asarray(arm_target, dtype=float)
        expected_torso = float(torso_target)
        if expected_arms.shape != (14,) or not np.all(np.isfinite(expected_arms)):
            raise ValueError("reset arm target must contain 14 finite values")
        if not np.isfinite(expected_torso):
            raise ValueError("reset body_j3 target must be finite")
        torso_index = int(self._torso_config["joint_index"])
        deadline = time.monotonic() + float(timeout_s)
        stable = 0
        arm_error = float("inf")
        torso_error = float("inf")
        while time.monotonic() < deadline:
            with self._hal_joint_states_lock:
                measured_arms = self.hal_full_body_joint_states[:14].copy()
                measured_torso = float(self.hal_full_body_joint_states[torso_index])
                feedback_age = time.monotonic() - self._hal_joint_states_last_monotonic
            if feedback_age <= self._feedback_timeout_s:
                arm_error = float(np.max(np.abs(measured_arms - expected_arms)))
                torso_error = abs(measured_torso - expected_torso)
                settled = (
                    arm_error <= float(arm_tolerance_deg)
                    and torso_error <= float(torso_tolerance_deg)
                )
                stable = stable + 1 if settled else 0
                if stable >= int(stable_samples):
                    print(
                        f"RESET_FEEDBACK_SETTLED arm_error={arm_error:.3f}deg "
                        f"body_j3_error={torso_error:.3f}deg"
                    )
                    return measured_arms
            time.sleep(0.02)
        print(
            f"[RESET BLOCKED] feedback did not settle within {timeout_s:.1f}s "
            f"(arm_error={arm_error:.3f}deg, body_j3_error={torso_error:.3f}deg); "
            "teleoperation remains paused"
        )
        return None

    def _finish_motion_reset(self, measured_arm_joints):
        """Atomically rebase every cached command to post-home feedback."""
        measured = np.asarray(measured_arm_joints, dtype=float)
        if measured.shape != (14,) or not np.all(np.isfinite(measured)):
            raise ValueError("post-reset feedback must contain 14 finite joints")
        with self._trajectory_lock:
            with self._hal_joint_states_lock:
                torso_pitch = float(
                    self.hal_full_body_joint_states[
                        int(self._torso_config["joint_index"])
                    ]
                )
            with self._both_arm_grip_lock:
                self._raw_arm_joints = measured.copy()
                self._last_raw_arm_joints = measured.copy()
                self._raw_torso_pitch = None
                self._torso_command_active = False
            with self._safety_lock:
                self._last_safe_left = measured[:7].copy()
                self._last_safe_right = measured[7:].copy()
                self._freeze_left_arm = False
                self._freeze_right_arm = False
                self._trajectory.reset(measured)
                self._last_safe_torso_pitch = torso_pitch
                self._torso_trajectory.reset([torso_pitch])
            self._torso_mapper.deactivate()
            if hasattr(self, "_torso_output_lock"):
                with self._torso_output_lock:
                    self._torso_output_trajectory.reset([torso_pitch])
                    self._torso_output_target = torso_pitch
                    self._torso_output_active = False
        self._reset_in_progress.clear()
        print("RESET_REBASE_OK Placo/controller/Ruckig/safety caches use live home feedback")


    def sync_placo_to_real(self, reset_or_not = False):
        """将真实机械臂的当前关节角度同步到 Placo 模型，并重置参考位姿"""
        print("=== 正在同步 Placo 模型到真实机械臂状态 ===")
        left_q, right_q = [-0.026,0.235,-0.225,0.002,0.232,0.024,0.227],[0,0,0,0,0,0,0]
        if (not reset_or_not):
            print("="*100,"初始化同步")
            left_q_delt = self.left_q_origin
            right_q_delt = self.right_q_origin
             
            left_q[0] =  np.deg2rad(  left_q_delt[0] - 180)
            left_q[1] =  np.deg2rad( -left_q_delt[1]  )
            left_q[2] =  np.deg2rad(  left_q_delt[2] )
            left_q[3] =  np.deg2rad( -left_q_delt[3] )
            # left_q[0] =  np.deg2rad( left_q_delt[0] - 90)
            # left_q[1] =  np.deg2rad(-left_q_delt[1] - 90)
            # left_q[2] =  np.deg2rad( left_q_delt[2] + 90)
            # left_q[3] =  np.deg2rad(-left_q_delt[3] - 90)            
            left_q[4] =  np.deg2rad(left_q_delt[4])
            left_q[5] =  np.deg2rad(left_q_delt[5]  )         #shang  xia     
            left_q[6] =  np.deg2rad(left_q_delt[6])

            right_q[0] = np.deg2rad( right_q_delt[0] )
            right_q[1] = np.deg2rad( right_q_delt[1] )
            right_q[2] = np.deg2rad( right_q_delt[2] )
            right_q[3] = np.deg2rad( right_q_delt[3] )
            # right_q[0] = np.deg2rad( right_q_delt[0] + 90)
            # right_q[1] = np.deg2rad( right_q_delt[1] + 90)
            # right_q[2] = np.deg2rad( right_q_delt[2] - 90)
            # right_q[3] = np.deg2rad(-right_q_delt[3] - 90)
            right_q[4] = np.deg2rad( right_q_delt[4])
            right_q[5] = np.deg2rad(-right_q_delt[5])
            right_q[6] = np.deg2rad(-right_q_delt[6])
        else:
            print("="*100,"复位键同步")
            left_q_delt = self.left_q_reset
            right_q_delt = self.right_q_reset
            
            left_q[0] =  np.deg2rad(  left_q_delt[0] - 180)
            left_q[1] =  np.deg2rad( -left_q_delt[1]  )
            left_q[2] =  np.deg2rad(  left_q_delt[2] )
            left_q[3] =  np.deg2rad( -left_q_delt[3] )
            # left_q[0] =  np.deg2rad( left_q_delt[0] - 90)
            # left_q[1] =  np.deg2rad(-left_q_delt[1] - 90)
            # left_q[2] =  np.deg2rad( left_q_delt[2] + 90)
            # left_q[3] =  np.deg2rad(-left_q_delt[3] - 90)            
            left_q[4] =  np.deg2rad(left_q_delt[4])
            left_q[5] =  np.deg2rad(left_q_delt[5]  )         #shang  xia     
            left_q[6] =  np.deg2rad(left_q_delt[6])

            right_q[0] = np.deg2rad( right_q_delt[0] )
            right_q[1] = np.deg2rad( right_q_delt[1] )
            right_q[2] = np.deg2rad( right_q_delt[2] )
            right_q[3] = np.deg2rad( right_q_delt[3] )
            # right_q[0] = np.deg2rad( right_q_delt[0] + 90)
            # right_q[1] = np.deg2rad( right_q_delt[1] + 90)
            # right_q[2] = np.deg2rad( right_q_delt[2] - 90)
            # right_q[3] = np.deg2rad(-right_q_delt[3] - 90)
            right_q[4] = np.deg2rad( right_q_delt[4])
            right_q[5] = np.deg2rad(-right_q_delt[5])
            right_q[6] = np.deg2rad(-right_q_delt[6])


        #腰7
        # 初始化机械臂关节
        self.placo_robot.state.q[12:19] = (left_q)
        self.placo_robot.state.q[19:26] = (right_q)
        print("left_arm:",self.placo_robot.state.q[12:19])
        print("right_arm:",self.placo_robot.state.q[19:26])
        
        # [DEBUG] 检查 URDF 限位
        for joint_id in range(self.placo_robot.model.njoints):
            q_idx = self.placo_robot.model.idx_qs[joint_id]
            if 12 <= q_idx <= 25:  # 只检查手臂关节
                joint_name = self.placo_robot.model.names[joint_id]
                q_val = self.placo_robot.state.q[q_idx]
                # 获取限位
                lower = self.placo_robot.model.lowerPositionLimit[q_idx] if hasattr(self.placo_robot.model, 'lowerPositionLimit') else None
                upper = self.placo_robot.model.upperPositionLimit[q_idx] if hasattr(self.placo_robot.model, 'upperPositionLimit') else None
                if lower is not None and upper is not None:
                    if q_val < lower or q_val > upper:
                        print(f"[DEBUG] LIMIT VIOLATION! {joint_name} q[{q_idx}]={q_val:.4f} limit=[{lower:.4f}, {upper:.4f}]")
        
        self.placo_robot.update_kinematics()
        self.sync_end_effector_poses_to_placo_tasks()

        for name in list(self.ref_ee_xyz.keys()):
            self.ref_ee_xyz[name] = None
            self.ref_controller_xyz[name] = None
            self.ref_ee_quat[name] = None
            self.ref_controller_quat[name] = None

        if hasattr(self, 'joints_regularization_task'):
            self.joints_regularization_task.set_joints(self._get_joint_values_dict())

        # if hasattr(self, 'placo_vis'):
        #     with self._viz_lock:
        #         self.placo_vis.display(self.placo_robot.state.q)

        print("✅ Placo 已完全同步并重置所有目标")
        print(f"Left joints (rad): {[round(float(x),3) for x in self.placo_robot.state.q[12:19]]}")

    def sync_placo_to_real_origin(self):
        """将真实机械臂的当前关节角度同步到 Placo 模型，并重置参考位姿"""
        print("=== 正在同步 Placo 模型到真实机械臂状态 ===")
        left_q, right_q = [-0.026,0.235,-0.225,0.002,0.232,0.024,0.227],[0,0,0,0,0,0,0]
        # 初始化腰部关节为0（body_joint1~5）
        self.placo_robot.state.q[2:7] = 0.0
        # 初始化头部关节为0（head_joint1~3）
        self.placo_robot.state.q[21:24] = 0.0
        # 初始化机械臂关节
        self.placo_robot.state.q[12:19] = np.deg2rad(left_q)
        self.placo_robot.state.q[19:26] = np.deg2rad(right_q)
        self.placo_robot.update_kinematics()
        self.sync_end_effector_poses_to_placo_tasks()
        for name in list(self.ref_ee_xyz.keys()):
            self.ref_ee_xyz[name] = None
            self.ref_controller_xyz[name] = None
            self.ref_ee_quat[name] = None
            self.ref_controller_quat[name] = None
        if hasattr(self, 'joints_regularization_task'):
            self.joints_regularization_task.set_joints(self._get_joint_values_dict())
        if hasattr(self, 'placo_vis'):
            with self._viz_lock:
                self.placo_vis.display(self.placo_robot.state.q)
        print("✅ Placo 已完全同步并重置所有目标")
        print(f"Left joints (rad): {[round(float(x),3) for x in self.placo_robot.state.q[12:19]]}")



    def _start_collection_thread(self):
        """Poll all PICO gestures through one deterministic state machine."""
        def collection_loop():
            while not self._stop_event.is_set():
                now = time.monotonic()
                a_pressed = self.xr_client.get_button_state_by_name("A")
                b_pressed = self.xr_client.get_button_state_by_name("B")
                x_pressed = self.xr_client.get_button_state_by_name("X")
                y_pressed = self.xr_client.get_button_state_by_name("Y")
                xy_decision = self._xy_recording_arbiter.update(
                    x_pressed, y_pressed, now
                )
                if xy_decision.reset_requested:
                    if not self._reset_request.is_set() and not self._reset_in_progress.is_set():
                        self._reset_request.set()
                        print(
                            "RESET_REQUEST_IMMEDIATE X+Y; recording state preserved="
                            f"{self._recording_state}"
                        )
                    else:
                        print("RESET_REQUEST_DUPLICATE_IGNORED")

                if xy_decision.start_recording:
                    if self._recording_state == "idle":
                        self.real_arm_command_sender.send_start_video_signal([1])
                        self._recording_state = "recording"
                        self.is_collecting = True
                        print("LEROBOT_RECORD_REQUEST_START (X double-click)")
                    else:
                        print(
                            "LEROBOT_RECORD_START_IGNORED state="
                            f"{self._recording_state}"
                        )
                if xy_decision.stop_recording:
                    if self._recording_state == "recording":
                        self.real_arm_command_sender.send_start_video_signal([0])
                        self._recording_state = "review"
                        self.is_collecting = False
                        self._review_button_arbiter.reset()
                        print(
                            "LEROBOT_RECORD_REQUEST_STOP (Y double-click); "
                            "B single=save, B double=discard"
                        )
                    else:
                        print(
                            "LEROBOT_RECORD_STOP_IGNORED state="
                            f"{self._recording_state}"
                        )

                if self._recording_state == "review":
                    review = self._review_button_arbiter.update(b_pressed, now)
                    if review.save:
                        self.real_arm_command_sender.send_start_video_signal([2])
                        self._recording_state = "idle"
                        print("LEROBOT_REVIEW_SAVE_REQUEST (B single-click)")
                    elif review.discard:
                        self.real_arm_command_sender.send_start_video_signal([3])
                        self._recording_state = "idle"
                        print("LEROBOT_REVIEW_DISCARD_REQUEST (B double-click)")

                # During the review prompt B belongs only to save/discard. At
                # all other times A+B remains the chassis deadman chord.
                motion_b = (
                    b_pressed
                    if self._recording_state not in {"finalizing", "review"}
                    else False
                )
                decision = self._ab_arbiter.update(
                    a_pressed, motion_b, now
                )
                with self._chassis_lock:
                    was_enabled = self._chassis_enabled
                    torso_was_enabled = self._torso_enabled
                    self._chassis_enabled = decision.chassis_enabled
                    self._torso_enabled = decision.torso_enabled
                if decision.chassis_enabled and not was_enabled:
                    print("CHASSIS_AB_ENABLED body pelvis velocity follow active")
                elif was_enabled and not decision.chassis_enabled:
                    print("CHASSIS_AB_RELEASED immediate deadman zero")
                if decision.torso_enabled and not torso_was_enabled:
                    print("TORSO_A_HOLD_ENABLED body_j3 relative pitch follow active")
                elif torso_was_enabled and not decision.torso_enabled:
                    print("TORSO_A_HOLD_RELEASED body_j3 target hold")

                try:
                    local_frame_pressed = bool(
                        self.xr_client.get_button_state_by_name("left_menu_button")
                    )
                    self._local_frame_button_fault_reported = False
                except Exception as exc:
                    local_frame_pressed = False
                    if not self._local_frame_button_fault_reported:
                        print(f"[LOCAL FRAME] button read failed: {exc}")
                        self._local_frame_button_fault_reported = True
                if local_frame_pressed and not self._local_frame_button_pressed:
                    with self._local_frame_lock:
                        self._local_frame_generation += 1
                        generation = self._local_frame_generation
                    print(
                        f"LOCAL_FRAME_RECENTER_REQUEST generation={generation} "
                        "(left three-bar menu button)"
                    )
                self._local_frame_button_pressed = local_frame_pressed

                self._stop_event.wait(0.005)
        self.collect_thread = threading.Thread(target=collection_loop, daemon=True)
        self.collect_thread.start()

    # ==================== 删除：_collect_data_if_active 函数已移除 ====================
    # 功能由 ROS2 pub 线程替代
    # ==================== 删除结束 ====================

    def _process_xr_pose(self, xr_pose, src_name):
        """Process the current XR controller pose."""
        if not getattr(self, "tcp_replay_mode", False):
            headset_pose = self.xr_client.get_pose_by_name("headset")
            controller_xyz, controller_rotation = pose_in_headset_heading_frame(
                xr_pose, headset_pose
            )
            rotation_transform = np.eye(4)
            rotation_transform[:3, :3] = controller_rotation
            controller_quat = tf.quaternion_from_matrix(rotation_transform)
        else:
            # Preserve the historical recorded-TCP replay coordinate contract.
            controller_xyz = self.R_headset_world @ np.asarray(xr_pose[:3], dtype=float)
            controller_quat = np.array(
                [xr_pose[6], xr_pose[3], xr_pose[4], xr_pose[5]], dtype=np.float64
            )
            q_norm = np.linalg.norm(controller_quat)
            if q_norm <= 1e-6:
                raise ValueError("recorded controller quaternion is invalid")
            controller_quat /= q_norm
            rotation_transform = np.eye(4)
            rotation_transform[:3, :3] = self.R_headset_world
            rotation_quat = tf.quaternion_from_matrix(rotation_transform)
            controller_quat = tf.quaternion_multiply(
                tf.quaternion_multiply(rotation_quat, controller_quat),
                tf.quaternion_conjugate(rotation_quat),
            )
        if self.ref_controller_xyz[src_name] is None:
            self.ref_controller_xyz[src_name] = controller_xyz
            self.ref_controller_quat[src_name] = controller_quat
            delta_xyz = np.zeros(3)
            delta_rot = np.array([0.0, 0.0, 0.0])
        else:
            delta_xyz = (controller_xyz - self.ref_controller_xyz[src_name]) * self.scale_factor
            delta_rot = quat_diff_as_angle_axis(self.ref_controller_quat[src_name], controller_quat)
        return delta_xyz, delta_rot

    def _placo_setup(self):
        """Set up the placo inverse kinematics solver."""
        self.placo_robot = placo.RobotWrapper(self.robot_urdf_path)
        print("Joint names in the Placo model:")
        for joint_name in self.placo_robot.model.names:
            print(f"  {joint_name}")
        self.solver = placo.KinematicsSolver(self.placo_robot)
        self.solver.dt = self.dt
        if self.q_init is not None:
            if self.floating_base:
                self.placo_robot.state.q = self.q_init.copy()
            else:
                self.solver.mask_fbase(True)
                self.placo_robot.state.q[7:] = self.q_init.copy()
        else:
            if not self.floating_base:
                self.solver.mask_fbase(True)
            self.placo_robot.state.q[:7] = np.array([0, 0, 0, 0, 0, 0, 1])
        self.placo_robot.update_kinematics()
        for name, config in self.manipulator_config.items():
            control_mode = config.get("control_mode", "pose")
            self.effector_control_mode[name] = control_mode
            ee_xyz, ee_quat = self._get_link_pose(config["link_name"])
            if control_mode == "position":
                self.effector_task[name] = self.solver.add_position_task(config["link_name"], ee_xyz)
                print(f"Created position task for {name} -> {config['link_name']}")
            else:
                ee_target = tf.quaternion_matrix(ee_quat)
                ee_target[:3, 3] = ee_xyz
                self.effector_task[name] = self.solver.add_frame_task(config["link_name"], ee_target)
                print(f"Created pose task for {name} -> {config['link_name']}")
            self.effector_task[name].configure(name, "soft", 10.0)
            manipulability = self.solver.add_manipulability_task(config["link_name"], "both", 1.0)
            manipulability.configure("manipulability", "soft", 1e-4)
            if "motion_tracker" in config:
                tracker_config = config["motion_tracker"]
                link_target = tracker_config["link_target"]
                target_xyz, _ = self._get_link_pose(link_target)
                tracker_task_name = f"{name}_tracker"
                self.motion_tracker_task[name] = self.solver.add_position_task(link_target, target_xyz)
                self.motion_tracker_task[name].configure(tracker_task_name, "soft", 1.0)
                print(f"Motion tracker position task created for {name} -> {link_target}")
        self.placo_robot.update_kinematics()     
        self.joints_regularization_task = self.solver.add_joints_task()
        self.joints_regularization_task.set_joints(self._get_joint_values_dict())
        self.joints_regularization_task.configure("joints_regularization", "soft", 1.0)  # 提高权重，防止solver跳到不同IK解
        
        # 固定非手臂关节（腰部、头部等），防止 solver 调整它们
        # 根据实测：左臂 q[12:19]，右臂 q[19:26]
        # 使用 idx_qs 获取正确的关节名称
        fixed_joints = {}
        arm_q_indices = set(range(12, 26))  # 左臂 + 右臂的 q 索引
        exclude_q_indices = {0, 1}  # 排除 universe 和 root_joint
        
        for joint_id in range(self.placo_robot.model.njoints):
            joint_name = self.placo_robot.model.names[joint_id]
            # 获取该关节在 q 中的起始索引
            q_idx = self.placo_robot.model.idx_qs[joint_id]
            # 检查是否在手臂范围外，且不是特殊关节
            if q_idx not in arm_q_indices and q_idx not in exclude_q_indices and q_idx >= 0:
                fixed_joints[joint_name] = self.placo_robot.state.q[q_idx]
        
        if fixed_joints:
            self.fixed_joints_task = self.solver.add_joints_task()
            self.fixed_joints_task.set_joints(fixed_joints)
            self.fixed_joints_task.configure("fixed_joints", "hard", 1.0)  # 硬约束，严格禁止非手臂关节移动
            print(f"Fixed {len(fixed_joints)} non-arm joints to prevent body movement")
            print(f"  Fixed joints: {list(fixed_joints.keys())}")
        print("Added joints regularization task to prevent drift when deactivated.")


    def deal_button_info(self):
        right_trigger = self.xr_client.get_key_value_by_name('right_trigger')
        left_trigger  = self.xr_client.get_key_value_by_name('left_trigger')
        print(f"右臂夹爪 trigger: {right_trigger:.3f}")
        print(f"左臂夹爪 trigger: {left_trigger:.3f}")

    # _update_ik_lxl, quat_to_euler_deg, euler_deg_to_quat, _update_ik,
    # _update_motion_tracker_tasks, _init_placo_viz, _update_placo_viz
    # 已迁移至 base_teleop_controller_ik_mixin.py (IkVizMixin)

    def _get_joint_values_dict(self):
        """正确映射关节名到q值，使用idx_qs避免浮动基座导致的偏移"""
        joint_values = {}
        for joint_id in range(self.placo_robot.model.njoints):
            joint_name = self.placo_robot.model.names[joint_id]
            q_idx = self.placo_robot.model.idx_qs[joint_id]
            if q_idx >= 2:  # 跳过 universe(0) 和 root_joint(1)，它们是浮动基座
                joint_values[joint_name] = float(self.placo_robot.state.q[q_idx])
        return joint_values

    def _get_joint_values_dict(self):
        """正确映射关节名到q值，使用idx_qs避免浮动基座导致的偏移"""
        joint_values = {}
        for joint_id in range(self.placo_robot.model.njoints):
            joint_name = self.placo_robot.model.names[joint_id]
            q_idx = self.placo_robot.model.idx_qs[joint_id]
            if q_idx >= 2:  # 跳过 universe(0) 和 root_joint(1)
                joint_values[joint_name] = float(self.placo_robot.state.q[q_idx])
        return joint_values

    def sync_end_effector_poses_to_placo_tasks(self):
        """
        Syncs the current end effector link poses to their corresponding placo tasks.
        This is useful for initializing or resetting task targets to current robot state.
        """
        for name, config in self.manipulator_config.items():
            ee_xyz, ee_quat = self._get_link_pose(config["link_name"])
            if self.effector_control_mode[name] == "position":
                self.effector_task[name].target_world = ee_xyz
            else:
                ee_target = tf.quaternion_matrix(ee_quat)
                ee_target[:3, 3] = ee_xyz
                self.effector_task[name].T_world_frame = ee_target
            print(f"Synced {name} end effector pose to placo task: {config['link_name']}")

    def _update_gripper_target(self):
        for gripper_name in self.manipulator_config.keys():
            if "gripper_config" not in self.manipulator_config[gripper_name]:
                continue
            gripper_config = self.manipulator_config[gripper_name]["gripper_config"]
            gripper_type = gripper_config["type"]
            if gripper_type == "parallel":
                trigger_value = self.xr_client.get_key_value_by_name(gripper_config["gripper_trigger"])
                for joint_name, open_pos, close_pos in zip(
                    gripper_config["joint_names"],
                    gripper_config["open_pos"],
                    gripper_config["close_pos"],
                ):
                    gripper_pos = calc_parallel_gripper_position(open_pos, close_pos, trigger_value)
                    self.gripper_pos_target[gripper_name][joint_name] = gripper_pos
                    self.gripper_pos_target[gripper_name][joint_name] = gripper_pos
            else:
                raise ValueError(f"Unsupported gripper type: {gripper_type}")

    def _log_data(self):
        """
        Logs the current state of the robot, including joint positions, end effector poses,
        and any other relevant data
        """
        if self.enable_log_data:
            raise NotImplementedError

    # ---------------------------------------------------------
    # --- Abstract Methods (to be implemented by subclasses) ---
    # ---------------------------------------------------------

    @abc.abstractmethod
    def _robot_setup(self):
        """Initializes the specific backend (connects to robot, starts sim, etc.)."""
        raise NotImplementedError

    @abc.abstractmethod
    def _update_robot_state(self):
        """Reads the current joint states from the robot/sim and updates self.placo_robot.state.q."""
        raise NotImplementedError

    @abc.abstractmethod
    def _send_command(self):
        """Sends the calculated target joint positions from self.placo_robot.state.q to the robot/sim."""
        raise NotImplementedError

    @abc.abstractmethod
    def _get_link_pose(self, link_name):
        """Gets the current world pose for a given link name."""
        raise NotImplementedError

    @abc.abstractmethod
    def run(self):
        """
        The main entry point. Subclasses must implement this to define their
        execution model (single-threaded or multi-threaded).
        """
        raise NotImplementedError

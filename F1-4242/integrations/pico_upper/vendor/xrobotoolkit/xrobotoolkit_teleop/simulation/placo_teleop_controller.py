import time
import threading
from typing import Any, Dict

import meshcat.transformations as tf

from xrobotoolkit_teleop.common.base_teleop_controller import BaseTeleopController
from xrobotoolkit_teleop.utils.geometry import (
    R_HEADSET_TO_WORLD,
)


class PlacoTeleopController(BaseTeleopController):
    """
    Placo teleoperation controller for a robot using inverse kinematics.

    This controller allows teleoperation of a robot by mapping XR controller poses
    to end effector movements using the Placo IK solver.
    """

    def __init__(
        self,
        robot_urdf_path: str,
        manipulator_config: Dict[str, Dict[str, Any]],
        floating_base=False,
        R_headset_world=R_HEADSET_TO_WORLD,
        scale_factor=1.0,
        q_init=None,
        dt=0.01,
    ):
        super().__init__(
            robot_urdf_path,
            manipulator_config,
            floating_base,
            R_headset_world,
            scale_factor,
            q_init,
            dt,
        )  
        print("[DEBUG] super().__init__() 完成，准备调用 _init_placo_viz()")
        try:
            self._init_placo_viz()
            print("[DEBUG] _init_placo_viz() 成功")
        except Exception as e:
            print(f"[ERROR] _init_placo_viz() 失败: {e}")
            import traceback
            traceback.print_exc()
        # 可视化放到独立线程，不阻塞控制主循环
        # self._viz_lock = threading.Lock()
        # self._viz_thread = threading.Thread(target=self._viz_loop, daemon=True)
        # self._viz_thread.start()
        # 这里初始化过程,可以加入自己的机械臂的初始化位置

    def _robot_setup(self):
        pass
 
    def _send_command(self):
        # 可视化已移至独立线程，主循环不再阻塞
        pass

    def _viz_loop(self):
        """独立可视化线程，以较低频率(10Hz)更新，减少与 DDS/UDP 的网络争抢"""
        viz_period = 1.0 / 30.0  # 10Hz 足够观察，大幅降低 WebSocket 负载
        while not self._stop_event.is_set():
            viz_start = time.time()
            try:
                with self._viz_lock:
                    self._update_placo_viz()
            except Exception as e:
                print(f"[VIZ] 可视化错误: {e}")  # 打印错误便于调试
            elapsed = time.time() - viz_start
            time.sleep(max(0, viz_period - elapsed))

    def _update_robot_state(self):
        pass

    def _get_link_pose(self, link_name):
        link_xyz = self.placo_robot.get_T_world_frame(link_name)[:3, 3]
        link_quat = tf.quaternion_from_matrix(self.placo_robot.get_T_world_frame(link_name))
        return link_xyz, link_quat

    def run(self):
        """
        Run the main teleoperation loop.
        使用高精度定时，确保控制频率稳定。
        """
        loop_count = 0
        freq_print_interval = 100  # 每100个循环打印一次实际频率
        freq_start_time = time.perf_counter()

        while not self._stop_event.is_set():
            try:
                loop_start = time.perf_counter()
                self._update_ik()
                self._send_command()

                # 高精度定时：用 perf_counter 确保周期稳定
                elapsed = time.perf_counter() - loop_start
                sleep_time = self.dt - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)
                else:
                    # 超时不sleep，但打印警告（降频诊断）
                    if loop_count % 500 == 0 and loop_count > 0:
                        print(f"[WARN] 主循环超时: {elapsed*1000:.1f}ms (目标 {self.dt*1000:.1f}ms)")

                # 频率统计
                loop_count += 1
                if loop_count % freq_print_interval == 0:
                    now = time.perf_counter()
                    actual_freq = freq_print_interval / (now - freq_start_time)
                    # print(f"[INFO] 实际控制频率: {actual_freq:.1f} Hz")
                    freq_start_time = now

            except KeyboardInterrupt:
                print("\nTeleoperation stopped.")
                self._stop_event.set()

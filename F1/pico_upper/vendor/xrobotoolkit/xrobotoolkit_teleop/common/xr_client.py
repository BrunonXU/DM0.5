import numpy as np
import xrobotoolkit_sdk as xrt
import threading
import time
from pico_f1.xr_readiness import XrReadiness


class XrClient:
    """Client for the XrClient SDK to interact with XR devices."""

    def __init__(self):
        """Initializes the XrClient and the SDK."""
        xrt.init()
        self._readiness = XrReadiness()
        self._readiness_lock = threading.RLock()
        self._readiness_checked = -1.0
        self._last_triggers = {'left_trigger': 0.0, 'right_trigger': 0.0}
        self._reported_state = None
        self.recovery_generation = 0
        print("XRoboToolkit SDK initialized.")

    def input_ready(self):
        """On reconnect require fresh frames and released controls, never replay held inputs."""
        with self._readiness_lock:
            now = time.monotonic()
            if now - self._readiness_checked < .005:
                return self._readiness.ready
            self._readiness_checked = now
            was_ready = self._readiness.ready
            try:
                poses = [xrt.get_left_controller_pose(), xrt.get_right_controller_pose(), xrt.get_headset_pose()]
                axes = [*xrt.get_left_axis(), *xrt.get_right_axis()]
                neutral = (all(np.isfinite(v) and abs(float(v)) <= .05 for v in axes)
                           and all(np.isfinite(v) and float(v) <= .05 for v in (
                               xrt.get_left_grip(), xrt.get_right_grip(),
                               xrt.get_left_trigger(), xrt.get_right_trigger()))
                           and not any(fn() for fn in (xrt.get_A_button, xrt.get_B_button,
                               xrt.get_X_button, xrt.get_Y_button, xrt.get_left_menu_button)))
                self._readiness.update(int(xrt.get_time_stamp_ns()), poses, neutral, now)
            except Exception as exc:
                self._readiness.update(0, [], False, now)
                self._readiness.last_fault = 'sdk_exception:' + type(exc).__name__
            state = self._readiness.reason
            if self._readiness.ready and not was_ready:
                self.recovery_generation += 1
            if state != self._reported_state:
                age = (None if self._readiness.changed_at is None else
                       round(1000*(now-self._readiness.changed_at), 1))
                print(f'XR_CONNECTION_STATE {state} stream_age_ms={age} '
                      f'last_fault={self._readiness.last_fault} '
                      f'fault_age_ms={self._readiness.last_fault_age_s*1000:.1f}; '
                      'lost stream: hold position; release controls and await XR_READY', flush=True)
                self._reported_state = state
            return self._readiness.ready

    def get_pose_by_name(self, name: str) -> np.ndarray:
        """Returns the pose of the specified device by name.
        Valid names: "left_controller", "right_controller", "headset".
        Pose is [x, y, z, qx, qy, qz, qw]."""
        if name == "left_controller":
            return xrt.get_left_controller_pose()
        elif name == "right_controller":
            return xrt.get_right_controller_pose()
        elif name == "headset":
            return xrt.get_headset_pose()
        else:
            raise ValueError(
                f"Invalid name: {name}. Valid names are: 'left_controller', 'right_controller', 'headset'."
            )

    def get_key_value_by_name(self, name: str) -> float:
        """Returns the trigger/grip value by name (float).
        Valid names: "left_trigger", "right_trigger", "left_grip", "right_grip".
        """
        if name not in ('left_trigger', 'right_trigger', 'left_grip', 'right_grip'):
            raise ValueError('invalid grip/trigger name: ' + name)
        if not self.input_ready():
            # Hold the gripper intent; a tracking drop must not open held objects.
            return self._last_triggers.get(name, 0.0)
        if name in self._last_triggers:
            value = float(xrt.get_left_trigger() if name == 'left_trigger' else xrt.get_right_trigger())
            self._last_triggers[name] = value
            return value
        elif name == "left_grip":
            return xrt.get_left_grip()
        elif name == "right_grip":
            return xrt.get_right_grip()
        else:
            raise ValueError(
                f"Invalid name: {name}. Valid names are: 'left_trigger', 'right_trigger', 'left_grip', 'right_grip'."
            )

    def get_button_state_by_name(self, name: str) -> bool:
        """Returns the button state by name (bool).
        Valid names: "A", "B", "X", "Y",
                      "left_menu_button", "right_menu_button",
                      "left_axis_click", "right_axis_click"
        """
        if not self.input_ready():
            return False
        if name == "A":
            return xrt.get_A_button()
        elif name == "B":
            return xrt.get_B_button()
        elif name == "X":
            return xrt.get_X_button()
        elif name == "Y":
            return xrt.get_Y_button()
        elif name == "left_menu_button":
            return xrt.get_left_menu_button()
        elif name == "right_menu_button":
            return xrt.get_right_menu_button()
        elif name == "left_axis_click":
            return xrt.get_left_axis_click()
        elif name == "right_axis_click":
            return xrt.get_right_axis_click()
        else:
            raise ValueError(
                f"Invalid name: {name}. Valid names are: 'A', 'B', 'X', 'Y', "
                "'left_menu_button', 'right_menu_button', 'left_axis_click', 'right_axis_click'."
            )

    def get_timestamp_ns(self) -> int:
        """Returns the current timestamp in nanoseconds (int)."""
        return xrt.get_time_stamp_ns() if self.input_ready() else 0

    def get_hand_tracking_state(self, hand: str) -> np.ndarray | None:
        """Returns the hand tracking state for the specified hand.
        Valid hands: "left", "right".
        State is a 27 x 7 numpy array, where each row is [x, y, z, qx, qy, qz, qw] for each joint.
        Returns None if hand tracking is inactive (low quality).
        """
        if hand.lower() == "left":
            if not xrt.get_left_hand_is_active():
                return None
            return xrt.get_left_hand_tracking_state()
        elif hand.lower() == "right":
            if not xrt.get_right_hand_is_active():
                return None
            return xrt.get_right_hand_tracking_state()
        else:
            raise ValueError(f"Invalid hand: {hand}. Valid hands are: 'left', 'right'.")

    def get_joystick_state(self, controller: str) -> list[float]:
        """Returns the joystick state for the specified controller.
        Valid controllers: "left", "right".
        State is a list with shape (2) representing [x, y] for each joystick.
        """
        if not self.input_ready():
            return [0.0, 0.0]
        if controller.lower() == "left":
            return xrt.get_left_axis()
        elif controller.lower() == "right":
            return xrt.get_right_axis()
        else:
            raise ValueError(f"Invalid controller: {controller}. Valid controllers are: 'left', 'right'.")

    def get_motion_tracker_data(self) -> dict:
        """Returns a dictionary of motion tracker data, where the keys are the tracker serial numbers.
        Each value is a dictionary containing the pose, velocity, and acceleration of the tracker.
        """
        num_motion_data = xrt.num_motion_data_available()
        if num_motion_data == 0:
            return {}

        poses = xrt.get_motion_tracker_pose()
        velocities = xrt.get_motion_tracker_velocity()
        accelerations = xrt.get_motion_tracker_acceleration()
        serial_numbers = xrt.get_motion_tracker_serial_numbers()

        tracker_data = {}
        for i in range(num_motion_data):
            serial = serial_numbers[i]
            tracker_data[serial] = {
                "pose": poses[i],
                "velocity": velocities[i],
                "acceleration": accelerations[i],
            }

        return tracker_data

    def get_body_tracking_data(self) -> dict | None:
        """Returns complete body tracking data or None if unavailable.

        Returns:
            Dict with keys: 'poses', 'velocities', 'accelerations', 'imu_timestamps', 'body_timestamp'
            - poses: (24, 7) array [x,y,z,qx,qy,qz,qw] for each joint
            - velocities: (24, 6) array [vx,vy,vz,wx,wy,wz] for each joint
            - accelerations: (24, 6) array [ax,ay,az,wax,way,waz] for each joint
        """
        if not self.input_ready() or not xrt.is_body_data_available():
            return None

        return {
            "poses": xrt.get_body_joints_pose(),
            "velocities": xrt.get_body_joints_velocity(),
            "accelerations": xrt.get_body_joints_acceleration(),
            "imu_timestamps": xrt.get_body_joints_timestamp(),
            "body_timestamp": xrt.get_body_timestamp_ns(),
        }

    def close(self):
        xrt.close()

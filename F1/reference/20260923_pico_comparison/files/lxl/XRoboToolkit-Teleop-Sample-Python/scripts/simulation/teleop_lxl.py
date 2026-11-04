import os

import tyro

from xrobotoolkit_teleop.simulation.placo_teleop_controller import (
    PlacoTeleopController,
)
from xrobotoolkit_teleop.utils.path_utils import ASSET_PATH

 
def main(
    # robot_urdf_path: str = os.path.join(ASSET_PATH, "flexiv/flexiv_Rizon4s_kinematics.urdf"),
    robot_urdf_path: str = os.path.join(ASSET_PATH, "/home/user/lxl/XRoboToolkit-Teleop-Sample-Python/assets/arx/f1_wheel/urdf/f1_wheel_0602_new.urdf"),
    # robot_urdf_path: str = os.path.join(ASSET_PATH, "/home/user/lxl/jushenzhineng/pico4ultra/XRoboToolkit-Teleop-Sample-Python/assets/arx/tianji_urdf/urdf/Marvin M6-S-R-CCS-696-V4.0 urdf_double_arm_lxlcopy.urdf"),
    scale_factor: float = 1.5,
):
    """
    Main function to run the Flexiv Rizon4s teleoperation with Placo.
    """
    config = {
        "right_hand": {
            "link_name": "arm_R_Link7",
            "pose_source": "right_controller",
            "control_trigger": "right_grip",
            "gripper_trigger": "right_trigger",
            # "control_mode": "position",  # Try "pose" for full pose control
            # "motion_tracker": {
            #     "serial": "PC2310BLH9020740B",
            #     "link_target": "Joint7_R",
            # },
        },
        "left_hand": {
            "link_name": "arm_L_Link7",
            "pose_source": "left_controller",
            "gripper_trigger": "left_trigger",
            "control_trigger": "left_grip",
            # "control_mode": "position",  # Try "pose" for full pose control
            # "motion_tracker": {
            #     "serial": "PC2310BLH9020707B",
            #     "link_target": "Joint7_L",
            # },
        },
    }

    # Create and initialize the teleoperation controller
    controller = PlacoTeleopController(
        robot_urdf_path=robot_urdf_path,
        manipulator_config=config,
        scale_factor=scale_factor,
    )

    # additional constraints hardcoded here for now
    # joints_task = controller.solver.add_joints_task()
    # joints_task.set_joints({joint: 0.0 for joint in controller.placo_robot.joint_names()})
    # joints_task.configure("joints_regularization", "soft", 1e-4)

    controller.run()


if __name__ == "__main__":
    tyro.cli(main)

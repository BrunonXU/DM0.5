"""Explicit upper-only entry; upstream all-in-one launcher is never used."""
import signal
import rclpy
from pico_f1.config import load_control_config, project_root
from xrobotoolkit_teleop.simulation.placo_teleop_controller import PlacoTeleopController

def main():
    config = load_control_config()
    manipulators = {
        side + '_hand': {
            'link_name': 'arm_' + letter + '_Link7',
            'pose_source': side + '_controller',
            'control_trigger': side + '_grip',
            'gripper_trigger': side + '_trigger',
        } for side, letter in [('left', 'L'), ('right', 'R')]
    }
    controller = None
    def stop(*_):
        if controller is None:
            raise KeyboardInterrupt
        controller._stop_event.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        controller = PlacoTeleopController(
            str(project_root() / 'assets/f1_wheel/urdf/f1_wheel_0602_new.urdf'),
            manipulators, scale_factor=float(config['cartesian_scale']),
            dt=1. / float(config['command_hz']))
        print('UPPER_TELEOP_READY Grip=independent Trigger=gripper Menu=recenter ABXY=disabled', flush=True)
        controller.run()
    finally:
        if controller is not None:
            controller._stop_event.set()
            controller.__del__()
        if rclpy.ok():
            rclpy.shutdown()

if __name__ == '__main__':
    main()

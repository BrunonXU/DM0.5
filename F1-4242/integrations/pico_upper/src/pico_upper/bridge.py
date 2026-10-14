"""Arm output worker: valid approved frames -> upstream 250 Hz arm resampler."""
import signal
import socket
import time
import rclpy
from sensor_msgs.msg import JointState
from rclpy.qos import QoSProfile, ReliabilityPolicy
from pico_f1.arm_output import ApprovedArmOutput
from pico_f1.config import load_control_config, arm_output_limits
from pico_upper.contract import ARM_NAMES, COMMAND_TOPICS, UDP_HOST, UDP_PORT, decode_frame
from pico_upper.ros_guard import GuardedNode

OWN_NODES = {'pico_upper_bridge', 'pico_upper_teleop'}

def foreign_publishers(node):
    return [f'{topic}:{info.node_namespace}/{info.node_name}'
            for topic in COMMAND_TOPICS for info in node.get_publishers_info_by_topic(topic)
            if info.node_name not in OWN_NODES]

def main():
    config = load_control_config()
    output = ApprovedArmOutput(**arm_output_limits(config))
    rclpy.init()
    node = GuardedNode('pico_upper_bridge')
    publisher = node.create_publisher(JointState, '/motion_ctl/joint_ctl',
                                    QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE))
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((UDP_HOST, UDP_PORT))  # Exclusive bind; no SO_REUSEADDR.
        sock.setblocking(False)
        print(f'UPPER_BRIDGE_READY {UDP_HOST}:{UDP_PORT} arms_only=true', flush=True)
        next_tick = time.monotonic()
        graph_check = next_tick
        try:
            while running and rclpy.ok():
                now = time.monotonic()
                if now >= graph_check:
                    conflicts = foreign_publishers(node)
                    if conflicts:
                        raise RuntimeError(f'EXTERNAL_CONTROL_CONFLICT {conflicts}')
                    graph_check = now + .5
                # Drain to the latest packet, bounded to protect servo scheduling.
                latest = None
                for _ in range(64):
                    try:
                        data, peer = sock.recvfrom(1024)
                    except BlockingIOError:
                        break
                    try:
                        latest = decode_frame(data, peer)
                    except ValueError as exc:
                        print(f'UPPER_PACKET_REJECTED {exc}', flush=True)
                if latest is not None:
                    output.push(latest[:14], time.monotonic(), tuple(bool(x) for x in latest[16:]))
                q = output.sample(time.monotonic())
                if q is not None:
                    msg = JointState()
                    msg.header.stamp = node.get_clock().now().to_msg()
                    msg.name, msg.position = list(ARM_NAMES), q.tolist()
                    publisher.publish(msg)
                rclpy.spin_once(node, timeout_sec=0)
                next_tick += .004
                if next_tick <= time.monotonic():
                    next_tick = time.monotonic() + .004
                time.sleep(max(0., next_tick - time.monotonic()))
        finally:
            # No reset, no body/base zero, no gripper open on shutdown.
            node.destroy_node()
            rclpy.shutdown()

if __name__ == '__main__':
    main()

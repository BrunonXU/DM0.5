"""Subscribe/discover and read ROS parameters only; no control publisher."""
import json
import time
import rclpy
from rcl_interfaces.srv import GetParameters

rclpy.init()
node = rclpy.create_node('f1_migration_readonly', enable_rosout=False, start_parameter_services=False)
result = {'hardware_published': False, 'parameters': {}, 'errors': {}}
try:
    until = time.monotonic()+4
    while time.monotonic() < until:
        rclpy.spin_once(node, timeout_sec=0.1)
    result['topics'] = node.get_topic_names_and_types()
    result['nodes'] = node.get_node_names_and_namespaces()
    for name, keys in {
        '/motor_ctl': ['arm_control_mode', 'arm_impedance_type', 'left_gripper_type',
                       'right_gripper_type', 'arm_l_j1_min', 'enable_udp_motors'],
        '/controller_node': ['max_speed.angular', 'max_acceleration.angular',
                             'max_deceleration.angular', 'max_speed.linear',
                             'max_acceleration.linear', 'max_deceleration.linear'],
    }.items():
        client = node.create_client(GetParameters, name+'/get_parameters')
        if not client.wait_for_service(timeout_sec=5):
            result['errors'][name] = 'service not discovered'
            continue
        future = client.call_async(GetParameters.Request(names=keys))
        rclpy.spin_until_future_complete(node, future, timeout_sec=5)
        if not future.done() or future.result() is None:
            result['errors'][name] = 'no parameter response'
            continue
        fields = {1:'bool_value', 2:'integer_value', 3:'double_value', 4:'string_value'}
        result['parameters'][name] = {key: getattr(v, fields[v.type]) if v.type in fields else None
                                            for key, v in zip(keys, future.result().values)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
finally:
    node.destroy_node()
    rclpy.shutdown()

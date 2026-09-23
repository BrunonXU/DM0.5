"""F1 console adapter. No ROS import, initialization or command on module import.

HAL angles are degrees, OmniPicker is 0=open/100=closed, base yaw is the
controller's scalar orientation.w (NOT a quaternion). Receipt monotonic time is
used for age; source headers are retained only as evidence. Manual joint paths
are small bounded joint-space interpolations, not collision checked paths.
"""
from collections import Counter
from copy import deepcopy
import math
from numbers import Real
import threading
import time
import uuid

ARM_NAMES = tuple(f'arm_{s}_j{i}' for s in ('l', 'r') for i in range(1, 8))
BODY_NAMES = tuple(f'body_j{i}' for i in range(1, 6))
HEAD_NAMES = tuple(f'head_j{i}' for i in range(1, 4))
DEFAULT_PROFILE = {
    'robot_id': 'F1-09', 'motion_enabled': False,
    'joints_enabled': False, 'limits_verified': False,
    'control_contract_verified': False, 'gripper_verified': False,
    'base_verified': False, 'expected_hard_auto': True,
    'feedback_max_age_s': 0.25, 'graph_max_age_s': 0.5,
    'graph_observation_s': 1.0, 'command_hz': 30.0,
    'max_joint_delta_deg': 5.0, 'max_joint_speed_deg_s': 5.0,
    'max_joint_acceleration_deg_s2': 15.0, 'joint_tolerance_deg': 1.0,
    'settle_timeout_s': 3.0, 'gripper_tolerance': 5.0,
    'gripper_timeout_s': 4.0, 'max_base_ttl_s': 0.3,
    'base_speed_limits': {'vx': 0.1, 'vy': 0.1, 'wz': 0.15},
    'joint_limits_deg': {}, 'base_frame': 'base_link',
    'topics': {
        'joints': '/hal/joint_states', 'joint_command': '/motion_ctl/joint_ctl',
        'left_gripper': '/motion_ctl/gripper/left/state',
        'right_gripper': '/motion_ctl/gripper/right/state',
        'left_gripper_command': '/motion_ctl/gripper/left',
        'right_gripper_command': '/motion_ctl/gripper/right',
        'base': '/move/State', 'base_command': '/move/ManualMoveCmd',
        'battery': '/driver/battery/Data',
    },
    'camera_topics': {
        name: f'/camera/{name}/color/image_raw/compressed'
        for name in ('head', 'left_wrist', 'right_wrist')
    },
}
COMMAND_KEYS = ('joint_command', 'left_gripper_command', 'right_gripper_command', 'base_command')


class ActionRejected(RuntimeError):
    """The requested action cannot continue; never interpret this as arrival."""


def finite(value, name):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ActionRejected(f'{name} 必须为有限数值')
    return float(value)


def source_stamp(msg):
    try:
        s = msg.header.stamp
        return {'sec': int(s.sec), 'nanosec': int(s.nanosec)}
    except (AttributeError, TypeError, ValueError):
        return None


class StateStore:
    """Thread-safe feedback, preserving independently arriving HAL joint groups."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.joints, self.grippers, self.base, self.battery, self.cameras = {}, {}, {}, {}, {}
        self.connected, self.error = False, None
        self.graph, self.graph_at = {}, None
        self.receipt_seq = 0

    def update_joints(self, msg):
        now, names, positions = self.clock(), list(msg.name), list(msg.position)
        counts = Counter(names)
        with self.lock:
            self.receipt_seq += 1
            for i, name in enumerate(names):
                if not isinstance(name, str) or not name:
                    continue
                try:
                    if counts[name] != 1 or i >= len(positions):
                        raise ActionRejected('无效关节字段')
                    value = finite(positions[i], name)
                except ActionRejected:
                    self.joints.pop(name, None)
                else:
                    self.joints[name] = {'position': value, 'received_at': now, 'receipt_seq': self.receipt_seq, 'source_stamp': source_stamp(msg)}

    def update_gripper(self, side, msg):
        name = f'{side}_gripper_position'
        with self.lock:
            self.receipt_seq += 1
            try:
                if list(msg.name).count(name) != 1:
                    raise ActionRejected('缺少夹爪名')
                value = finite(msg.position[list(msg.name).index(name)], name)
                if not 0 <= value <= 100:
                    raise ActionRejected('夹爪反馈超出0..100')
            except (ActionRejected, IndexError):
                self.grippers.pop(side, None)
            else:
                self.grippers[side] = {'position': value, 'received_at': self.clock(), 'receipt_seq': self.receipt_seq, 'source_stamp': source_stamp(msg)}

    def update_base(self, msg):
        with self.lock:
            try:
                values = {k: finite(v, k) for k, v in (
                    ('vx', msg.speed.linear.x), ('vy', msg.speed.linear.y), ('wz', msg.speed.angular.z))}
                for name in ('is_soft_auto', 'is_hard_auto', 'is_parking_mode', 'is_avoid'):
                    value = getattr(msg, name)
                    if type(value) is not bool:
                        raise ActionRejected('底盘模式字段无效')
                    values[name] = value
            except (AttributeError, ActionRejected):
                self.base = {}
                return
            # move/State uses odometry.position and scalar odometry.orientation.w.
            # Missing pose stays absent; no invented origin or quaternion conversion.
            try:
                values['pose'] = {k: finite(v, k) for k, v in (
                    ('x', msg.odometry.position.x), ('y', msg.odometry.position.y), ('yaw', msg.odometry.orientation.w))}
            except (AttributeError, ActionRejected):
                pass
            self.base = {**values, 'received_at': self.clock(), 'source_stamp': source_stamp(msg)}

    def update_battery(self, msg):
        rows = []
        for item in msg.data:
            row = {}
            for k in ('id', 'energy', 'health', 'is_in_place', 'is_charging', 'voltage',
                      'current', 'temperature', 'is_connected', 'error_code'):
                value = getattr(item, k, None)
                if isinstance(value, bool) or isinstance(value, str):
                    row[k] = value
                elif isinstance(value, Real) and math.isfinite(value):
                    row[k] = value
            rows.append(row)
        with self.lock:
            self.battery = {'data': rows, 'received_at': self.clock(), 'source_stamp': source_stamp(msg)}

    def update_camera(self, name, msg):
        data = bytes(msg.data)
        if not data or len(data) > 12 * 1024 * 1024:
            return
        # Only formats browsers decode; never label arbitrary ROS bytes JPEG.
        mime = 'image/jpeg' if data.startswith(b'\xff\xd8') else ('image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else None)
        if mime is None:
            return
        with self.lock:
            self.cameras[name] = {'data': data, 'mime': mime, 'format': str(msg.format),
                                  'received_at': self.clock(), 'source_stamp': source_stamp(msg)}

    def snapshot(self):
        now = self.clock()
        def age(item):
            return {k: deepcopy(v) for k, v in item.items() if k not in ('data', 'received_at')} | {
                'age_s': max(0.0, now-item['received_at'])}
        with self.lock:
            return {
                'connected': self.connected, 'error': self.error,
                'joints': {k: age(v) for k, v in self.joints.items()},
                'grippers': {k: age(v) for k, v in self.grippers.items()},
                'base': age(self.base) if self.base else {},
                'battery': ({**age(self.battery), 'data': deepcopy(self.battery['data'])} if self.battery else {}),
                'cameras': {k: {**age(v), 'size': len(v['data'])} for k, v in self.cameras.items()},
                'graph': deepcopy(self.graph),
                'graph_age_s': None if self.graph_at is None else max(0.0, now-self.graph_at),
            }

    def camera(self, name):
        with self.lock:
            item = self.cameras.get(name)
            return None if item is None else (item['data'], item['mime'], max(0.0, self.clock()-item['received_at']))


class RosTransport:
    """ROS joins a private context; all subscriptions, never implicit publishers."""
    def __init__(self, profile, store):
        self.profile, self.store = profile, store
        self.node = self.context = self.executor = None
        self.publishers, self.own_gids = {}, {}
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.started_at = None
        self.node_name = 'f1_console_' + uuid.uuid4().hex

    def start(self):
        import rclpy
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import qos_profile_sensor_data
        from rclpy.signals import SignalHandlerOptions
        from sensor_msgs.msg import JointState, CompressedImage
        from control_msgs.msg import GripperCommand
        from geometry_msgs.msg import TwistStamped
        from move.msg import State
        self.rclpy = rclpy
        self.types = {'joint_command': JointState, 'left_gripper_command': GripperCommand,
                      'right_gripper_command': GripperCommand, 'base_command': TwistStamped}
        self.context = Context()
        rclpy.init(args=[], context=self.context, signal_handler_options=SignalHandlerOptions.NO)
        self.node = rclpy.create_node(self.node_name, context=self.context, use_global_arguments=False,
                                      enable_rosout=False, start_parameter_services=False)
        self.executor = SingleThreadedExecutor(context=self.context)
        self.executor.add_node(self.node)
        topics = self.profile['topics']
        bindings = [(JointState, topics['joints'], self.store.update_joints),
                    (State, topics['base'], self.store.update_base)]
        for side in ('left', 'right'):
            bindings.append((JointState, topics[f'{side}_gripper'],
                             lambda m, s=side: self.store.update_gripper(s, m)))
        for name, topic in self.profile['camera_topics'].items():
            bindings.append((CompressedImage, topic, lambda m, n=name: self.store.update_camera(n, m)))
        try:
            from driver.msg import BatteriesData
        except ImportError:
            self.battery_available = False
        else:
            self.battery_available = True
            bindings.append((BatteriesData, topics['battery'], self.store.update_battery))
        self.subscriptions = [self.node.create_subscription(t, topic, cb, qos_profile_sensor_data)
                              for t, topic, cb in bindings]
        self.started_at = self.store.clock()
        self.store.connected = True
        self.thread = threading.Thread(target=self._spin, name='console-ros', daemon=True)
        self.thread.start()

    def _spin(self):
        next_graph = 0
        try:
            while not self.stop.is_set() and self.context.ok():
                self.executor.spin_once(timeout_sec=0.02)
                now = self.store.clock()
                if now >= next_graph:
                    self.refresh_graph()
                    next_graph = now + 0.1
        except Exception as exc:
            with self.store.lock:
                self.store.error = f'ROS反馈线程退出: {type(exc).__name__}: {exc}'
        finally:
            self.store.connected = False

    @staticmethod
    def _endpoint(info):
        gid = bytes(info.endpoint_gid).hex()
        if not gid or not any(bytes(info.endpoint_gid)):
            gid = None
        return {'name': info.node_name, 'namespace': info.node_namespace,
                'type': info.topic_type, 'gid': gid}

    def refresh_graph(self):
        with self.lock:
            graph = {}
            for key in COMMAND_KEYS:
                topic = self.profile['topics'][key]
                graph[key] = {'topic': topic,
                              'publishers': [self._endpoint(i) for i in self.node.get_publishers_info_by_topic(topic)],
                              'subscribers': [self._endpoint(i) for i in self.node.get_subscriptions_info_by_topic(topic)]}
            with self.store.lock:
                self.store.graph, self.store.graph_at = graph, self.store.clock()

    def assert_exclusive(self, key, require_subscriber=True):
        self.refresh_graph()
        with self.store.lock:
            entry = deepcopy(self.store.graph.get(key, {}))
        if require_subscriber and not entry.get('subscribers'):
            raise ActionRejected('命令没有可发现的控制器订阅者: ' + key)
        own = self.own_gids.get(key)
        external = [p for p in entry.get('publishers', []) if not own or p['gid'] != own]
        if external:
            raise ActionRejected('其他控制发布者存在（含unknown节点），拒绝抢占: ' + key)

    def prepare(self, key):
        with self.lock:
            self.assert_exclusive(key)
            if key in self.publishers:
                return
            self.publishers[key] = self.node.create_publisher(self.types[key], self.profile['topics'][key], 1)
            # Bind the newly created endpoint GID, not node-name based exclusion.
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                self.refresh_graph()
                entries = self.store.graph[key]['publishers']
                own = [e for e in entries if e['name'] == self.node_name and e['namespace'] == self.node.get_namespace() and e['gid']]
                if len(entries) == len(own) == 1:
                    self.own_gids[key] = own[0]['gid']
                    return
                if any(e['name'] != self.node_name for e in entries):
                    break
                time.sleep(0.02)
            self.release(key)
            raise ActionRejected('无法独立确认自身命令endpoint GID')

    def publish(self, key, values):
        with self.lock:
            msg = self.types[key]()
            if key == 'joint_command':
                msg.header.stamp = self.node.get_clock().now().to_msg()
                msg.header.frame_id = self.profile['base_frame']
                msg.name, msg.position = list(values), list(values.values())
            elif key == 'base_command':
                msg.header.stamp = self.node.get_clock().now().to_msg()
                msg.header.frame_id = self.profile['base_frame']
                msg.twist.linear.x, msg.twist.linear.y, msg.twist.angular.z = values
            else:
                msg.position, msg.max_effort = values, 0.0
            self.publishers[key].publish(msg)

    def release(self, key):
        with self.lock:
            pub = self.publishers.pop(key, None)
            if pub is not None and self.node is not None:
                self.node.destroy_publisher(pub)
            self.own_gids.pop(key, None)

    def close(self):
        self.stop.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        for key in list(self.publishers):
            self.release(key)
        if self.executor:
            self.executor.shutdown(timeout_sec=1)
        if self.node:
            self.node.destroy_node()
        if self.context and self.context.ok():
            self.context.shutdown()
        self.store.connected = False


class RosAdapter:
    def __init__(self, profile=None, *, clock=time.monotonic, transport=None):
        self.profile = deepcopy(DEFAULT_PROFILE)
        for key, value in (profile or {}).items():
            if key in ('topics', 'base_speed_limits'):
                self.profile[key].update(value)
            else:
                self.profile[key] = deepcopy(value)
        self.clock = clock
        self.store = StateStore(clock)
        self.transport = transport or RosTransport(self.profile, self.store)
        self._action_lock = threading.Lock()
        self._base_lock = threading.Lock()
        self._base_request = None
        self._active_cancel = None
        self._started_at = None

    def start(self):
        if self._started_at is not None:
            raise RuntimeError('adapter已经启动')
        try:
            self.transport.start()
            self._started_at = self.clock()
        except Exception as exc:
            self.store.error = f'{type(exc).__name__}: {exc}'
            self.transport.close()
            raise

    def close(self):
        if self._active_cancel:
            self._active_cancel.set()
        # The owner should cancel and join its action before close; this wait is
        # bounded and never starts a new stop command on an unused publisher.
        if self._action_lock.acquire(timeout=5):
            try:
                self.transport.close()
            finally:
                self._action_lock.release()
        else:
            raise RuntimeError('动作未退出，保留ROS上下文以便完成停止')

    def snapshot(self):
        return self.store.snapshot()

    def camera(self, name):
        return self.store.camera(name)

    def _common(self):
        snap = self.snapshot()
        if not snap['connected']:
            raise ActionRejected('ROS尚未连接或订阅线程已经退出')
        if not self.profile['motion_enabled']:
            raise ActionRejected('本机配置为只读；尚未启用动作')
        if not self.profile['control_contract_verified']:
            raise ActionRejected('本机控制模式/单位/限位合同尚未核验')
        if self._started_at is None or self.clock()-self._started_at < self.profile['graph_observation_s']:
            raise ActionRejected('DDS发现观察时间未完成')
        if snap['graph_age_s'] is None or snap['graph_age_s'] > self.profile['graph_max_age_s']:
            raise ActionRejected('DDS图状态缺失或过期')
        return snap

    def _fresh(self, item, label):
        if not item or item.get('age_s', math.inf) > self.profile['feedback_max_age_s']:
            raise ActionRejected(label + '反馈缺失或过期')
        return item

    def _joint_target(self, spec, snap):
        if not self.profile['joints_enabled'] or not self.profile['limits_verified']:
            raise ActionRejected('本机具名关节限位/手动控制未启用')
        targets = spec.get('targets')
        if not isinstance(targets, dict) or not targets or len(targets) > 22:
            raise ActionRejected('targets需要非空具名关节目标，禁止隐式零位')
        # The driver expects a complete vector for each selected 7-DOF arm.
        # Hold unselected members using fresh measured values, never zeros.
        targets = dict(targets)
        for group in (ARM_NAMES[:7], ARM_NAMES[7:], BODY_NAMES, HEAD_NAMES):
            if any(name in targets for name in group):
                for name in group:
                    if name not in targets:
                        targets[name] = self._fresh(snap['joints'].get(name), name)['position']
        current, values = {}, {}
        for name, value in targets.items():
            bounds = self.profile['joint_limits_deg'].get(name)
            if name not in ARM_NAMES + BODY_NAMES + HEAD_NAMES or not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
                raise ActionRejected('该关节无本机已核验限位: ' + str(name))
            value = finite(value, name)
            low, high = (finite(x, name+'限位') for x in bounds)
            if low >= high or not low <= value <= high:
                raise ActionRejected('目标超出本机关节限位: ' + name)
            origin = finite(self._fresh(snap['joints'].get(name), name)['position'], name)
            if not low <= origin <= high:
                raise ActionRejected('当前反馈超出已核验限位: ' + name)
            if abs(value-origin) > self.profile['max_joint_delta_deg']:
                raise ActionRejected(f'{name} 超过单次手动位移上限 {self.profile["max_joint_delta_deg"]} 度；全身预设尚未做路径验收')
            current[name], values[name] = origin, value
        duration = finite(spec.get('duration_s', 2.0), 'duration_s')
        if not 0.25 <= duration <= 10:
            raise ActionRejected('duration_s必须在0.25..10秒')
        delta = max(abs(values[n]-current[n]) for n in values)
        # Quintic minimum jerk: max speed 1.875*d/T, max accel < 6*d/T².
        if 1.875*delta/duration > self.profile['max_joint_speed_deg_s'] or 6*delta/duration**2 > self.profile['max_joint_acceleration_deg_s2']:
            raise ActionRejected('动作时长不满足本机手动速度/加速度上限，请增加时长')
        return current, values, duration

    def _base_spec(self, spec, snap):
        if not self.profile['base_verified']:
            raise ActionRejected('底盘手动模式/制动合同尚未核验')
        base = self._fresh(snap['base'], '底盘')
        if base.get('is_soft_auto') is not False or base.get('is_hard_auto') is not self.profile['expected_hard_auto']:
            raise ActionRejected('底盘不在已核验手动模式；本服务不切换模式')
        if base.get('is_parking_mode') is not False or base.get('is_avoid') is not False:
            raise ActionRejected('底盘停车或避障状态，拒绝运动')
        velocity = tuple(finite(spec.get(k), k) for k in ('vx', 'vy', 'wz'))
        if any(abs(v) > self.profile['base_speed_limits'][k] for k, v in zip(('vx','vy','wz'), velocity)):
            raise ActionRejected('底盘点动超过配置速度上限')
        ttl = finite(spec.get('ttl_s', 0.25), 'ttl_s')
        if not 0 < ttl <= min(0.3, self.profile['max_base_ttl_s']):
            raise ActionRejected('底盘TTL必须在0..0.3秒内')
        return velocity, ttl

    def capabilities(self):
        snap = self.snapshot()
        try:
            self._common()
            reason = None
        except ActionRejected as exc:
            reason = str(exc)
        available = {}
        for kind, key, enabled in (
            ('joints', 'joint_command', self.profile['joints_enabled'] and self.profile['limits_verified']),
            ('gripper', 'left_gripper_command', self.profile['gripper_verified']),
            ('base', 'base_command', self.profile['base_verified'])):
            why = reason or (None if enabled else '本机能力合同尚未核验/启用')
            peers = snap['graph'].get(key, {}).get('publishers', [])
            own = getattr(self.transport, 'own_gids', {}).get(key)
            external = [p for p in peers if not own or p.get('gid') != own]
            if external:
                why = '已有其他控制发布者（含unknown）；只读可用'
            available[kind] = {'available': why is None, 'reason': why}
        return {**available, 'collision_checked': False, 'units': {'joints': 'deg', 'gripper': '0_open_100_closed', 'base': 'm/s,rad/s'},
                'manual_limit_deg': self.profile['max_joint_delta_deg'],
                'stop_semantics': '停止自身输出；非硬件急停。关节仅在仍独占且反馈新鲜时发送一次实测保持。',
                'recording': {'available': False, 'reason': '数采及上传逻辑保持原样，未接管'}}

    def renew_base(self, spec):
        snap = self._common()
        velocity, ttl = self._base_spec(spec, snap)
        self.transport.assert_exclusive('base_command')
        with self._base_lock:
            if self._base_request is None or self.clock() >= self._base_request[1]:
                return False
            self._base_request = (velocity, self.clock()+ttl)
            return True

    def execute(self, spec, cancel_event, on_progress=None):
        if not self._action_lock.acquire(blocking=False):
            raise ActionRejected('已有动作，先结束或取消')
        self._active_cancel = cancel_event
        requested_at = self.clock()
        key, sent, hold = None, False, False
        kind = spec.get('kind') if isinstance(spec, dict) else None
        try:
            snap = self._common()
            if cancel_event.is_set():
                return {'status': 'cancelled', 'commands_sent': False}
            if kind == 'joints':
                origin, targets, duration = self._joint_target(spec, snap)
                key = 'joint_command'
            elif kind == 'gripper':
                if not self.profile['gripper_verified']:
                    raise ActionRejected('OmniPicker合同未核验')
                side = spec.get('side')
                if side not in ('left', 'right'):
                    raise ActionRejected('side必须为left或right')
                target = finite(spec.get('position'), 'gripper.position')
                if not 0 <= target <= 100:
                    raise ActionRejected('夹爪目标必须在0..100')
                self._fresh(snap['grippers'].get(side), side+'夹爪')
                key = side+'_gripper_command'
            elif kind == 'base':
                velocity, ttl = self._base_spec(spec, snap)
                key = 'base_command'
            else:
                raise ActionRejected('未知动作类型')
            self.transport.assert_exclusive(key)
            if cancel_event.is_set():
                return {'status': 'cancelled', 'commands_sent': False}
            self.transport.prepare(key)
            if cancel_event.is_set():
                return {'status': 'cancelled', 'commands_sent': False}
            if kind == 'joints':
                origin, targets, duration = self._joint_target(spec, self._common())
            started, arrival_receipts = self.clock(), None
            if kind == 'base':
                with self._base_lock:
                    self._base_request = (velocity, requested_at+ttl)
            timeout = (duration+self.profile['settle_timeout_s'] if kind == 'joints'
                       else self.profile['gripper_timeout_s'] if kind == 'gripper' else math.inf)
            while True:
                if cancel_event.is_set():
                    break
                if kind == 'base':
                    with self._base_lock:
                        if self._base_request is None or self.clock() >= self._base_request[1]:
                            self._base_request = None
                            break
                # DDS discovery can take time. All temporal checks and feedback
                # reads below must happen again AFTER that operation.
                self._common()
                self.transport.assert_exclusive(key)
                if cancel_event.is_set():
                    break
                snap = self._common()
                now = self.clock()
                if now-started > timeout:
                    raise ActionRejected('动作已下发但实测未在时限内到位，不能记为成功')
                if kind == 'joints':
                    items = {n: self._fresh(snap['joints'].get(n), n) for n in targets}
                    actual = {n: finite(item['position'], n) for n, item in items.items()}
                    progress = min(1.0, (now-started)/duration)
                    blend = 10*progress**3-15*progress**4+6*progress**5
                    command = {n: origin[n]+(targets[n]-origin[n])*blend for n in targets}
                    if cancel_event.is_set():
                        break
                    sent = True  # A publish exception can mean delivery unknown.
                    self.transport.publish(key, command)
                    arrived = progress >= 1 and all(abs(actual[n]-targets[n]) <= self.profile['joint_tolerance_deg'] for n in targets)
                elif kind == 'gripper':
                    items = {side: self._fresh(snap['grippers'].get(side), side+'夹爪')}
                    actual = finite(items[side]['position'], 'gripper feedback')
                    if cancel_event.is_set():
                        break
                    sent = True
                    self.transport.publish(key, target)
                    progress = None
                    arrived = abs(actual-target) <= self.profile['gripper_tolerance']
                else:
                    # Serialize renewal with the final TTL/velocity read and
                    # dispatch. Never publish a pre-discovery cached velocity.
                    with self._base_lock:
                        request = self._base_request
                        if cancel_event.is_set() or request is None or self.clock() >= request[1]:
                            self._base_request = None
                            break
                        velocity, deadline = request
                        self._base_spec({'vx': velocity[0], 'vy': velocity[1], 'wz': velocity[2], 'ttl_s': 0.1}, self._common())
                        if cancel_event.is_set() or self.clock() >= deadline:
                            self._base_request = None
                            break
                        sent = True
                        self.transport.publish(key, velocity)
                    progress, arrived = None, False
                if on_progress:
                    on_progress({'elapsed_s': now-started, 'progress': progress, 'fraction': progress, 'feedback_arrived': arrived})
                if arrived:
                    # Arrival requires distinct RECEIVED samples spanning the
                    # settle interval for EVERY target joint/selected gripper.
                    # Re-reading one cached sample can never verify arrival.
                    receipts = {n: (item.get('receipt_seq'), now-item['age_s']) for n,item in items.items()}
                    if any(seq is None for seq,_ in receipts.values()):
                        raise ActionRejected('反馈缺少接收序号，不能确认连续到位')
                    if arrival_receipts is None:
                        arrival_receipts = receipts
                    elif all(seq != arrival_receipts[n][0] and received-arrival_receipts[n][1] >= 0.15
                             for n,(seq,received) in receipts.items()):
                        return {'status': 'succeeded', 'commands_sent': True, 'feedback_verified': True, 'elapsed_s': now-started}
                else:
                    arrival_receipts = None
                cancel_event.wait(1.0 / self.profile['command_hz'])
            if kind == 'joints' and sent:
                try:
                    self.transport.assert_exclusive(key)
                    snap = self._common()
                    actual = {n: finite(self._fresh(snap['joints'].get(n), n)['position'], n) for n in targets}
                    self.transport.publish(key, actual)
                    hold = True
                except ActionRejected:
                    pass
            return {'status': 'cancelled' if cancel_event.is_set() else 'completed',
                    'commands_sent': sent, 'hold_sent': hold, 'feedback_verified': False}
        except Exception as exc:
            # HTTP must distinguish rejection-before-command from failure after
            # dispatch (including an exception with unknown delivery outcome).
            exc.commands_sent = sent
            exc.hold_sent = hold
            raise
        finally:
            # Zero is sent only if this action actually commanded the base.
            # TTL is bounded locally even if the browser disappears. This does
            # not replace the physical emergency stop or guarantee base arrival.
            try:
                if key == 'base_command' and sent:
                    for _ in range(3):
                        self.transport.publish(key, (0.0, 0.0, 0.0))
                        time.sleep(0.01)
            except Exception as exc:
                exc.commands_sent = sent
                exc.hold_sent = hold
                raise
            finally:
                with self._base_lock:
                    self._base_request = None
                try:
                    if key:
                        self.transport.release(key)
                except Exception as exc:
                    exc.commands_sent = sent
                    exc.hold_sent = hold
                    raise
                finally:
                    self._active_cancel = None
                    self._action_lock.release()

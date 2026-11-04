"""Local-odometry XY return, base only. No motion on import/status/capture.

F1 move/State.odometry.orientation.w is yaw in radians, NOT a quaternion.
No SLAM/world-position claim; rotation requires the explicit 4F-F1 option.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import signal
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import socket
import time

from .cli import runtime_lock
from .ros_backend import RosBackend, _finite
from .runtime import ReplayError, base_stopped, check_command_peers, check_command_activity, profile_issues

ROOT = Path('/home/user/Bruno/F1/foam_sort_whole_body')
POLICY = dict(speed_m_s=0.10, acceleration_m_s2=0.08, position_tolerance_m=0.025,
              heading_tolerance_rad=math.radians(2), max_return_distance_m=3.0,
              timeout_s=120.0, max_anchor_age_s=3600.0, gain=0.5, hz=20.0)
YAW_POLICY = dict(speed_rad_s=.05, minimum_speed_rad_s=.035, acceleration_rad_s2=.05,
                  tolerance_rad=math.radians(.3), max_initial_error_rad=math.radians(15),
                  settle_s=.5)


def angle(value):
    return math.atan2(math.sin(value), math.cos(value))


def error_to_target(current, target):
    dx, dy = target[0]-current[0], target[1]-current[1]
    c, s = math.cos(current[2]), math.sin(current[2])
    return c*dx+s*dy, -s*dx+c*dy, angle(target[2]-current[2])


def velocity(current, target, previous, dt):
    ex, ey, yaw = error_to_target(current, target)
    distance = math.hypot(ex, ey)
    if abs(yaw) > POLICY['heading_tolerance_rad']:
        raise ReplayError('朝向偏差超过2度；本版仅平移归位，不自动旋转')
    if distance > POLICY['max_return_distance_m']:
        raise ReplayError('起点距离超过3米近距离归位范围')
    if distance <= POLICY['position_tolerance_m']:
        return (0.0, 0.0, 0.0)
    speed = min(POLICY['speed_m_s'], POLICY['gain']*distance,
                math.sqrt(2*POLICY['acceleration_m_s2']*distance))
    wanted = (speed*ex/distance, speed*ey/distance)
    delta = [wanted[i]-previous[i] for i in range(2)]
    norm = math.hypot(*delta)
    ratio = min(1.0, POLICY['acceleration_m_s2']*min(dt, .1)/max(norm, 1e-12))
    return previous[0]+ratio*delta[0], previous[1]+ratio*delta[1], 0.0


def validate_anchor(anchor, boot_id, host, now, machine_id=None):
    if anchor.get('schema') != 'f1_base_home_odometry_v1':
        raise ReplayError('不是本脚本保存的里程计起点')
    if anchor.get('boot_id') != boot_id or anchor.get('host') != host:
        raise ReplayError('机器人已重启或备份主机不同；请重新保存起点')
    age = now-_finite(anchor.get('created_unix_s'), '起点时间')
    # Identified anchors belong to a robot boot, not a wall-clock hour. NTP
    # corrections do not change the saved local-odometry coordinate system.
    if machine_id is not None:
        if not machine_id or anchor.get('machine_id') != machine_id:
            raise ReplayError('里程计起点与本机machine-id不符')
    elif not 0 <= age <= POLICY['max_anchor_age_s']:
        raise ReplayError('起点超过1小时或系统时间变化；请重新保存起点')
    target = anchor.get('xy_yaw')
    if not isinstance(target, list) or len(target) != 3:
        raise ReplayError('起点坐标不完整')
    return tuple(_finite(x, '起点坐标') for x in target)


class YawThenXY:
    """Separate rotation/standstill/translation using the existing velocity interface."""
    def __init__(self, current, target):
        error = error_to_target(current, target)
        if abs(error[2]) > YAW_POLICY['max_initial_error_rad']:
            raise ReplayError('朝向差超过15度，超出本版小角度home恢复范围')
        if math.hypot(*error[:2]) > POLICY['max_return_distance_m']:
            raise ReplayError('起点距离超过3米近距离归位范围')
        self.phase = 'align_yaw'
        self.settled_since = None

    def step(self, current, target, previous, dt, now, stopped):
        error = error_to_target(current, target)
        if self.phase == 'align_yaw':
            if abs(error[2]) > YAW_POLICY['max_initial_error_rad']:
                raise ReplayError('恢复朝向期间偏差超过核定小角度范围')
            if abs(error[2]) <= YAW_POLICY['tolerance_rad']:
                if stopped:
                    if self.settled_since is None: self.settled_since = now
                    if now-self.settled_since >= YAW_POLICY['settle_s']:
                        self.phase = 'return_xy'
                else: self.settled_since = None
                return (0., 0., 0.)
            self.settled_since = None
            speed = min(YAW_POLICY['speed_rad_s'], max(YAW_POLICY['minimum_speed_rad_s'],
                        min(.8*abs(error[2]), math.sqrt(2*YAW_POLICY['acceleration_rad_s2']*abs(error[2])))))
            wanted = math.copysign(speed, error[2])
            change = YAW_POLICY['acceleration_rad_s2']*min(max(dt,0),.1)
            wz = max(previous[2]-change, min(previous[2]+change,wanted))
            return (0., 0., wz)
        if abs(error[2]) > YAW_POLICY['tolerance_rad']:
            # Stop translation fully before attempting another heading correction.
            if stopped:
                self.phase = 'align_yaw'
                self.settled_since = None
            return (0., 0., 0.)
        return velocity(current, target, previous, dt)


class HomeBackend(RosBackend):
    def __init__(self, profile):
        super().__init__(profile)
        self.odometry = None
        self.odom_fault = None
        self.server = None
        self._last_peer_check = 0.0

    def open(self):
        super().open()
        from server.msg import State
        from rclpy.qos import qos_profile_sensor_data
        self._subscriptions.append(self._node.create_subscription(
            State, '/server/State', self._on_server, qos_profile_sensor_data))

    def _on_server(self, msg):
        self.server = (time.monotonic(), msg)

    def _on_base_state(self, msg):
        super()._on_base_state(msg)
        try:
            pose = tuple(_finite(v, '里程计') for v in (
                msg.odometry.position.x, msg.odometry.position.y, msg.odometry.orientation.w))
            stamp = msg.header.stamp.sec*10**9 + msg.header.stamp.nanosec
            if stamp <= 0:
                raise ValueError('无效里程计时钟')
            now = time.monotonic()
            if self.odometry:
                old = self.odometry
                if stamp < old['stamp_ns']:
                    raise ValueError('里程计时钟倒退')
                dt = max(now-old['received'], 0)
                if math.hypot(pose[0]-old['pose'][0], pose[1]-old['pose'][1]) > .10+2*dt:
                    raise ValueError('里程计跳变或被重置')
            self.odometry = dict(pose=pose, received=now, stamp_ns=stamp,
                                 header_frame=msg.header.frame_id,
                                 joystick=msg.is_joystick_on, handle=msg.is_handle_on)
        except (ValueError, AttributeError) as exc:
            self.odom_fault = str(exc)

    def enable_commands(self):
        """Create ONLY the base publisher; reuse existing command-ownership monitor."""
        from .command_monitor import publisher_gid
        self._require_open()
        pub = self._node.create_publisher(self._message_types['base_command'],
            self.profile['topics']['base_command'], self._command_qos)
        self._publishers['base_command'] = pub
        self._own_command_gids = {'base_command': publisher_gid(pub)}
        self._commands_enabled = True

    def command_activity(self):
        result = super().command_activity()
        result['own_gids_bound'] = bool(self._own_command_gids.get('base_command'))
        return result

    def send(self, vector):
        if not self.commands_enabled:
            raise ReplayError('未启用底盘命令')
        yaw_allowed = self.profile.get('home_align_yaw', False)
        if (any(not math.isfinite(x) for x in vector)
                or (vector[2] != 0 and (not yaw_allowed or abs(vector[2]) > YAW_POLICY['speed_rad_s']+1e-9
                    or math.hypot(*vector[:2]) > 1e-9))
                or math.hypot(*vector[:2]) > POLICY['speed_m_s']+1e-9):
            raise ReplayError('归位命令超过本版纯平移限速')
        return self._publish_batch('base_home', [self._base_message(vector, self._stamp())])

    def check(self, stopped=False):
        snap = self.snapshot()
        b = snap.get('base', {})
        now = time.monotonic()
        if self.odom_fault:
            raise ReplayError(self.odom_fault)
        if not self.odometry or now-self.odometry['received'] > self.profile['feedback_max_age_s']:
            raise ReplayError('里程计反馈缺失或过旧')
        if not b or now-b['received_ns']/1e9 > self.profile['feedback_max_age_s']:
            raise ReplayError('底盘反馈缺失或过旧')
        if b['is_hard_auto'] is not True or b['is_soft_auto'] is not False:
            raise ReplayError('需要硬自动true、软自动false；不自动改变控制模式')
        if b['is_parking_mode'] or b['is_avoid'] or self.odometry['joystick'] or self.odometry['handle']:
            raise ReplayError('驻车/避障/手柄接管，停止归位')
        if self.server is None or now-self.server[0] > .5:
            raise ReplayError('server状态缺失或过旧')
        server = self.server[1]
        if not server.is_normal or server.is_scram or server.is_busy or server.has_task or server.is_relative_moving or server.is_charging:
            raise ReplayError('机器人异常/急停或已有任务；不接管其他人的控制')
        check_command_activity(self, self.profile)
        if now-self._last_peer_check >= .5:
            check_command_peers(self, self.profile, enabled=self.commands_enabled)
            self._last_peer_check = now
        if stopped and not base_stopped(snap, self.profile, time.monotonic_ns()):
            raise ReplayError('底盘未静止')
        return self.odometry['pose']


def pump(backend, seconds):
    until = time.monotonic()+seconds
    while time.monotonic() < until:
        backend.poll(min(.02, max(0, until-time.monotonic())))


def stop(backend):
    """Repeated zero, require sustained fresh measured standstill; never touch arms."""
    until = time.monotonic()+backend.profile['stop_timeout_s']
    stable = None
    while time.monotonic() < until:
        backend.stop_base()
        pump(backend, 1/backend.profile['stop_publish_hz'])
        if base_stopped(backend.snapshot(), backend.profile, time.monotonic_ns()):
            stable = stable or time.monotonic()
            if time.monotonic()-stable >= .5:
                return True
        else:
            stable = None
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['status', 'capture', 'return'], nargs='?', default='status')
    parser.add_argument('--profile', required=True, type=Path)
    parser.add_argument('--anchor', type=Path, default=ROOT/'runtime/base_home/start.json')
    parser.add_argument('--align-yaw', action='store_true', help='4F-F1: small-angle yaw alignment before XY return')
    parser.add_argument('--yaw-only', action='store_true', help='Restore saved heading at the current position')
    args = parser.parse_args(argv)
    if args.yaw_only and not args.align_yaw:
        parser.error('--yaw-only requires --align-yaw')
    backend = None
    locks = ExitStack()
    report = {'mode': args.mode, 'motion_started': False, 'scope': 'local_odometry_xy_not_slam_absolute', 'policy': POLICY}
    output = ROOT/'runtime/base_home/runs'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+str(os.getpid()))
    try:
        profile = json.loads(args.profile.read_text())
        if socket.gethostname() != profile.get('expected_host') or socket.gethostname() != 'ms-thor':
            raise ReplayError('只能在已核定F1主机运行')
        if profile.get('expected_machine_id') and Path('/etc/machine-id').read_text().strip() != profile['expected_machine_id']:
            raise ReplayError('底盘归位配置与本机machine-id不符')
        if args.align_yaw:
            if profile.get('expected_machine_id') != '00000000000000000000000000000001':
                raise ReplayError('朝向恢复候选只绑定4F-F1')
            profile['home_align_yaw'] = True
            report['yaw_policy'] = YAW_POLICY
            report['return_sequence'] = 'align_yaw_stop_return_xy'
        problems = profile_issues(profile)
        if problems:
            raise ReplayError('；'.join(problems))
        if not args.anchor.resolve().is_relative_to(ROOT/'runtime/base_home'):
            raise ReplayError('起点备份只能存放在本任务runtime/base_home内')
        if args.anchor.is_symlink():
            raise ReplayError('起点文件不能是软链接')
        output.mkdir(parents=True)
        backend = HomeBackend(profile)
        backend.open()
        pump(backend, 2)
        current = backend.check(stopped=True)
        report['current_xy_yaw'] = current
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        locks.enter_context(runtime_lock(output))
        if args.mode == 'capture':
            anchor = dict(schema='f1_base_home_odometry_v1', boot_id=boot_id,
                machine_id=Path('/etc/machine-id').read_text().strip(),
                host=socket.gethostname(), created_unix_s=time.time(), xy_yaw=list(current),
                odom=backend.odometry, scope=report['scope'])
            args.anchor.parent.mkdir(parents=True, exist_ok=True)
            # Capture is deliberate; preserve every earlier anchor before replacing latest.
            if args.anchor.exists():
                (output/'previous_anchor.json').write_bytes(args.anchor.read_bytes())
            (output/'captured_anchor.json').write_text(json.dumps(anchor, indent=2)+'\n')
            temp = args.anchor.with_name(args.anchor.name+'.'+str(os.getpid())+'.tmp')
            temp.write_text(json.dumps(anchor, indent=2)+'\n');temp.replace(args.anchor)
            report['saved_anchor'] = str(args.anchor)
        else:
            if not args.anchor.exists():
                if args.mode == 'return':
                    raise ReplayError('没有回放前起点；不能用现在的位置冒充过去起点')
                report['anchor_present'] = False
            else:
                anchor = json.loads(args.anchor.read_text())
                if profile.get('expected_machine_id') and anchor.get('machine_id') != profile['expected_machine_id']:
                    raise ReplayError('里程计起点未绑定本机machine-id，请本机重新capture')
                target = validate_anchor(anchor, boot_id, socket.gethostname(), time.time(),
                                         profile.get('expected_machine_id'))
                if profile.get('expected_machine_id'):
                    report['anchor_validity'] = 'same_machine_same_boot'
                    report['policy'] = dict(POLICY, max_anchor_age_s=None)
                if args.yaw_only:
                    report['saved_home_xy_yaw'] = target
                    target = (current[0], current[1], target[2])
                    report['return_sequence'] = 'align_yaw_only'
                report['target_xy_yaw'] = target
                report['error_body_xy_yaw'] = error_to_target(current, target)
                controller = YawThenXY(current, target) if args.align_yaw else None
                if controller is None: velocity(current, target, (0,0,0), .05)
                if args.mode == 'return':
                    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
                    print(('仅原地恢复保存的朝向。' if args.yaw_only else '先低速恢复朝向（≤0.05rad/s），停稳后XY归位（≤0.10m/s）。' if args.align_yaw else '仅底盘平移归位，最高0.10m/s。')+'无绕障规划；现场确认扫过空间畅通。')
                    input('按 Enter 开始；Ctrl-C 取消：')
                    pump(backend, 1)
                    current = backend.check(stopped=True)
                    if args.yaw_only:
                        target = (current[0], current[1], target[2])
                    if args.align_yaw: controller = YawThenXY(current, target)
                    else: velocity(current, target, (0,0,0), .05)
                    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt('SIGTERM')))
                    backend.enable_commands()
                    report['motion_started'] = True
                    pump(backend, .2)
                    deadline = time.monotonic()+POLICY['timeout_s']
                    previous = (0.0,0.0,0.0)
                    last = time.monotonic()
                    stable = None
                    with (output/'events.jsonl').open('w') as events:
                        while time.monotonic() < deadline:
                            pump(backend, 1/POLICY['hz'])
                            now = time.monotonic()
                            if now-last > .2:
                                raise ReplayError('归位调度停顿超过200ms')
                            current = backend.check()
                            measured_stopped = base_stopped(backend.snapshot(),profile,time.monotonic_ns())
                            vector = (controller.step(current,target,previous,now-last,now,measured_stopped)
                                      if controller else velocity(current,target,previous,now-last))
                            if args.yaw_only:
                                vector = (0., 0., vector[2])
                            backend.send(vector)
                            events.write(json.dumps(dict(t=now, pose=current, target=target, command=vector,
                                                         phase=controller.phase if controller else 'return_xy'))+'\n')
                            events.flush()
                            previous, last = vector, now
                            error = error_to_target(current,target)
                            yaw_reached = not controller or (controller.phase=='return_xy' and abs(error[2]) <= YAW_POLICY['tolerance_rad'])
                            if (args.yaw_only or math.hypot(*error[:2]) <= POLICY['position_tolerance_m']) and yaw_reached and measured_stopped:
                                stable = stable or now
                                if now-stable >= .5:
                                    report['target_reached_in_odometry'] = True
                                    break
                            else:
                                stable = None
                        else:
                            raise ReplayError('归位超时，已停止尝试')
    except (Exception, KeyboardInterrupt) as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        if backend:
            if backend.commands_enabled:
                try:report['zero_velocity_feedback_confirmed'] = stop(backend)
                except Exception as exc:report['stop_error'] = str(exc)
                if not report.get('zero_velocity_feedback_confirmed'):
                    report['error'] = report.get('error', '')+'；未确认停车，现场急停'
                elif report.get('target_reached_in_odometry'):
                    try:
                        final_pose = backend.check(stopped=True)
                        report['final_xy_yaw'] = final_pose
                        report['final_error_body_xy_yaw'] = error_to_target(final_pose, target)
                        if not args.yaw_only and math.hypot(*report['final_error_body_xy_yaw'][:2]) > POLICY['position_tolerance_m']:
                            raise ReplayError('停车后里程计仍超出归位容差')
                        if args.align_yaw and abs(report['final_error_body_xy_yaw'][2]) > YAW_POLICY['tolerance_rad']:
                            raise ReplayError('停车后朝向仍超出归位容差')
                    except Exception as exc:
                        report['target_reached_in_odometry'] = False
                        report['error'] = str(exc)
            try:
                backend.close()
            except Exception as exc:
                report['error'] = report.get('error', '')+'；关闭ROS失败：'+str(exc)
        locks.close()
        if output.exists():
            report['output'] = str(output)
            (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report.get('error') else 0


if __name__ == '__main__':
    raise SystemExit(main())

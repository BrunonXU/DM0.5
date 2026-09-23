"""Pair the existing pose restore and local-odometry return for repeat trials."""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import time
import uuid
from datetime import datetime, timezone
from . import base_home, pose_cli
from .cli import runtime_lock
from .pose_model import capture_pose
from .runtime import ReplayError

ROOT = base_home.ROOT


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_session(index, machine_id, boot_id, require_same_boot=True):
    if index.get('machine_id') != machine_id:
        raise ReplayError('起点来自其他机器人，不能用于本机')
    if require_same_boot and index.get('boot_id') != boot_id:
        raise ReplayError('机器人已重启，旧底盘里程计起点不可直接沿用；'
                          '可先用restore-pose恢复保存的手臂姿态，现场回到起点后再capture')
    directory = Path(index['directory'])
    if not directory.resolve().is_relative_to(ROOT/'runtime/base_home/4F-F1'):
        raise ReplayError('起点路径不属于4F-F1本任务')
    for name, digest in index['files'].items():
        if Path(name).name != name or sha(directory/name) != digest:
            raise ReplayError('起点备份哈希不一致')
    return directory


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['capture','status','align','return-base','restore-pose','restore'])
    p.add_argument('--config', required=True, type=Path)
    args = p.parse_args(argv)
    config = json.loads(args.config.read_text())
    profile_path = Path(config['profile'])
    profile = json.loads(profile_path.read_text())
    machine = Path('/etc/machine-id').read_text().strip()
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if machine != profile.get('expected_machine_id') or socket.gethostname() != profile['expected_host']:
        raise ReplayError('重复试验配置不属于本机')
    root = ROOT/'runtime/base_home/4F-F1'
    latest = root/'latest.json'
    if args.mode == 'capture':
        directory = root/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8])
        directory.mkdir(parents=True)
        backend = base_home.HomeBackend(profile)
        try:
            with runtime_lock(directory):
                backend.open()
                base_home.pump(backend, 2)
                first = None
                until = time.monotonic()+1
                while time.monotonic() < until:
                    base_home.pump(backend, .02)
                    xy_yaw = backend.check(stopped=True)
                    pose = capture_pose(backend.snapshot(), profile, now_ns=time.monotonic_ns(),
                                        host=socket.gethostname(), profile_sha256=sha(profile_path))
                    pose_cli.feedback_ok(pose, profile)
                    if first is None:
                        first = pose
                    if any(abs(value-first['joint_positions_deg'][name]) > .1 for name,value in pose['joint_positions_deg'].items()):
                        raise ReplayError('关节尚未静止，未绑定试验起点')
                anchor = dict(schema='f1_base_home_odometry_v1', boot_id=boot, machine_id=machine,
                              host=socket.gethostname(), created_unix_s=time.time(), xy_yaw=list(xy_yaw),
                              odom=backend.odometry, scope='local_odometry_xy_not_slam_absolute')
                (directory/'pose.json').write_text(json.dumps(pose, indent=2)+'\n')
                (directory/'anchor.json').write_text(json.dumps(anchor, indent=2)+'\n')
                config.update(baseline_pose=str(directory/'pose.json'), baseline_pose_sha256=sha(directory/'pose.json'))
                (directory/'restore.json').write_text(json.dumps(config, indent=2)+'\n')
                index = dict(schema='f1_trial_home_v1', machine_id=machine, boot_id=boot, directory=str(directory),
                             files={name:sha(directory/name) for name in ('pose.json','anchor.json','restore.json')},
                             hardware_published=False, control_publishers_created=False,
                             scope=('local_odom_xy_yaw_and_arms14' if config.get('restore_arms_only') else
                                    'arms14_body5_grippers2_and_local_odom_xy_yaw; head_saved_not_restored'))
                (directory/'manifest.json').write_text(json.dumps(index, indent=2)+'\n')
                temp = root/('latest.'+uuid.uuid4().hex+'.tmp')
                temp.write_text(json.dumps(index, indent=2)+'\n');temp.replace(latest)
                print(json.dumps(index, ensure_ascii=False, indent=2))
        finally:
            backend.close()
        return 0
    if not latest.exists():
        raise ReplayError('没有试验前的本机起点；先在现场摆好后capture')
    index = json.loads(latest.read_text())
    directory = validate_session(index, machine, boot, args.mode != 'restore-pose')
    if args.mode != 'restore-pose':
        anchor = json.loads((directory/'anchor.json').read_text())
        base_home.validate_anchor(anchor, boot, socket.gethostname(), time.time(), machine)
    if args.mode in ('status','align','return-base','restore'):
        yaw_args = ['--align-yaw'] if config.get('align_yaw_on_return') else []
        if args.mode == 'align':
            yaw_args = ['--align-yaw', '--yaw-only']
        code = base_home.main(['status' if args.mode == 'status' else 'return', '--profile',str(profile_path), '--anchor',str(directory/'anchor.json'), *yaw_args])
        if code: return code
    if args.mode in ('status','restore-pose','restore'):
        scope_args = ['--arms-only'] if config.get('restore_arms_only') else []
        if config.get('restore_arm_tracking_wait_s'):
            scope_args += ['--arm-tracking-wait-s',str(config['restore_arm_tracking_wait_s'])]
        return pose_cli.main(['plan-restore' if args.mode == 'status' else 'restore', '--config',str(directory/'restore.json'), *scope_args])
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (Exception, KeyboardInterrupt) as exc:
        print(f'F1 home: {type(exc).__name__}: {exc}')
        raise SystemExit(2)

"""Compare an identical V52 read-only sensor subscriber across production launch environments."""
import argparse
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
import shlex
import subprocess
import sys


HERE = Path(__file__).resolve().parent
NATIVE = '/home/user/Bruno/DAgger-runtime/f1-09-venv/bin/python'
V52 = '/home/user/Bruno/HARNESS/development/04bc818d7424'
FIELD_SITE = '/home/user/Bruno/HARNESS/site/web-bcfd8730efd14981bcd2d7429858808b'
JAZZY = '/opt/ros/jazzy/setup.bash'
OWNER_SETUPS = (JAZZY, '/home/user/workspace/f1/install/setup.bash', '/home/user/workspace/agv/install/setup.bash')
CAMERA_SETUPS = ('/home/user/workspace/camera/OrbbecSDK_ROS2/install/setup.bash',
                 '/home/user/workspace/camera/ros2_ws/install/setup.bash')
MODES = ('production_service', 'production_owner_environment', 'owner_with_camera_overlays')
RESULT = 'F1_READONLY_RESULT '


def sample(bundle, site, duration):
    # Sent down SSH stdin. No owner, geometry worker, motion publisher or START path.
    from collections import Counter
    import faulthandler
    import hashlib
    import json
    import os
    from pathlib import Path
    import sys
    import threading
    import time

    faulthandler.enable()
    faulthandler.dump_traceback_later(duration + 10, file=sys.stderr)
    root = Path(bundle)
    sys.path[:0] = [str(root / p) for p in ('deps', 'src', 'control/src', 'upstream/RPent')]
    from f1_harness.ros_observer import RosObserver
    from f1_harness.evidence import FrameCache
    from rclpy.utilities import get_rmw_implementation_identifier

    manifest = json.loads((root / 'manifest.json').read_text())
    gateway = Path(site) / 'gateway.local.json'
    profile = json.loads(gateway.read_bytes())['observation']
    streams = {}
    topics = {}
    for camera in [*profile['cameras'], *profile.get('inspection_cameras', [])]:
        for stream in ('rgb', 'depth'):
            if stream not in camera:
                continue
            key = camera['name'] + '.' + stream
            spec = camera[stream]
            streams[key] = (camera['name'], stream)
            topics[key] = spec['topic']
            topics[key + '.info'] = spec['camera_info_topic']
            if stream == 'rgb' and spec['type'] == 'raw':
                topics[key + '.recording'] = spec['topic'] + '/compressed'
    if profile.get('joint_state'):
        topics['joint_state'] = profile['joint_state']['topic']
    observer = RosObserver(profile, FrameCache())
    errors = []

    def spin():
        try:
            observer.spin()
        except Exception as exc:
            errors.append(f'{type(exc).__name__}: {exc}')
    worker = threading.Thread(target=spin, daemon=True)
    worker.start()
    distinct = {name: set() for name in streams}
    pairs = Counter()
    faults = Counter()
    samples = []
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < duration:
            time.sleep(.2)
            observed = observer.observe()
            now = time.monotonic_ns()
            frames = {}
            for frame in observed.frames:
                key = frame.camera + '.' + frame.stream
                distinct[key].add(frame.sequence)
                frames[key] = {'sequence': frame.sequence, 'age_ms': round((now - frame.received_ns) / 1e6, 2)}
            qualities = {pair.camera: pair.quality for pair in observed.pairings}
            pairs.update([camera + '.' + quality for camera, quality in qualities.items()])
            faults.update(observed.faults)
            samples.append({'at_s': round(time.monotonic() - t0, 3), 'frames': frames, 'pairing': qualities,
                            'callback_counts': {key: observer.sequences.get(stream, 0) for key, stream in streams.items()}})
        pubs = {topic: observer.node.count_publishers(topic) for topic in sorted(set(topics.values()))}
        env_keys = ('RMW_IMPLEMENTATION', 'CYCLONEDDS_URI', 'ROS_AUTOMATIC_DISCOVERY_RANGE',
                    'ROS_DOMAIN_ID', 'ROS_LOCALHOST_ONLY', 'ROS_STATIC_PEERS', 'AMENT_PREFIX_PATH', 'PYTHONPATH')
        result = {'bundle_id': manifest['bundle_id'], 'gateway_config_sha256': hashlib.sha256(gateway.read_bytes()).hexdigest(),
                  'rmw': get_rmw_implementation_identifier(), 'environment': {k: os.environ.get(k) for k in env_keys},
                  'seconds': round(time.monotonic() - t0, 3), 'spin_alive': worker.is_alive(), 'errors': errors,
                  'distinct_images': {k: len(v) for k, v in distinct.items()}, 'pairs': dict(pairs),
                  'faults': dict(faults), 'callback_counts': samples[-1]['callback_counts'],
                  'topics': topics, 'publisher_counts': pubs, 'info_streams': sorted('.'.join(k) for k in observer.info),
                  'samples': samples, 'command_publishers': 0, 'start_sent': False}
        print('F1_READONLY_RESULT ' + json.dumps(result), flush=True)
    finally:
        observer.close()
        worker.join(2.)


def launch(mode, seconds):
    # V52 site.supervise spawns the gateway with this filtered environment.
    child = [NATIVE, '-', V52, FIELD_SITE, str(seconds)]
    if mode == 'production_service':
        filter_env = """import os,sys;from pathlib import Path
root=Path(sys.argv[1]);sys.path[:0]=[str(root/'deps'),str(root/'src')]
from f1_harness.site import _safe_environment
os.execve(sys.argv[2],sys.argv[2:],_safe_environment())"""
        cmd = ['source ' + JAZZY, 'exec ' + shlex.join([NATIVE, '-c', filter_env, V52, *child])]
    else:
        setups = OWNER_SETUPS + (CAMERA_SETUPS if mode == 'owner_with_camera_overlays' else ())
        cmd = ['source ' + shlex.quote(path) + ' || exit' for path in setups]
        cmd += ['export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp',
                'export CYCLONEDDS_URI=/etc/ros/cyclonedds/cyclonedds.xml',
                'export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET',
                'unset ROS_LOCALHOST_ONLY ROS_STATIC_PEERS', 'exec ' + shlex.join(child)]
    return shlex.join(['timeout', '--kill-after=3', str(seconds + 25), 'bash', '-c', '\n'.join(cmd)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=12, choices=range(5, 61), metavar='5-60')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--modes', nargs='+', choices=MODES, default=MODES[:2])
    parser.add_argument('--direct-host', choices=['192.168.31.142', '192.168.31.58'])
    args = parser.parse_args()
    out = args.output or HERE / ('dds-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    out.mkdir(parents=True, exist_ok=False)
    print('receipts:', out, flush=True)
    config = json.loads((HERE.parents[1] / 'site/workspace.local.json').read_text())
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'ConnectTimeout=8',
           '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + config['known_hosts'], '-i', config['identity']]
    if args.direct_host:
        alias = '[' + config['host'].split('@')[-1] + ']:' + str(config['port'])
        ssh += ['-o', 'HostKeyAlias=' + alias, config['host'].split('@')[0] + '@' + args.direct_host]
    else:
        ssh += ['-p', str(config['port']), config['host']]
    script = inspect.getsource(sample) + '\nimport sys\nsample(sys.argv[1], sys.argv[2], int(sys.argv[3]))\n'
    failed = False
    summary = []
    for index, mode in enumerate(args.modes, 1):
        name = f'{index:02d}-{mode}'
        argv = ssh + [launch(mode, args.seconds)]
        try:
            process = subprocess.run(argv, input=script, capture_output=True, text=True, timeout=args.seconds + 40)
            code, stdout, stderr = process.returncode, process.stdout, process.stderr
        except subprocess.TimeoutExpired as exc:
            code = 124
            stdout = (exc.stdout or b'').decode(errors='replace')
            stderr = (exc.stderr or b'').decode(errors='replace') + '\n6000D SSH watchdog expired'
        (out / (name + '.stderr')).write_text(stderr)
        result = {'sample_received': False}
        for line in stdout.splitlines():
            if line.startswith(RESULT):
                result = json.loads(line[len(RESULT):])
                result['sample_received'] = True
        result.update(mode=mode, returncode=code)
        (out / (name + '.json')).write_text(json.dumps(result, indent=2) + '\n')
        row = {k: result.get(k) for k in ('mode', 'returncode', 'sample_received', 'rmw', 'callback_counts', 'pairs', 'errors')}
        summary.append(row)
        print(json.dumps(row), flush=True)
        if code or not result['sample_received'] or result.get('errors') or not result.get('spin_alive'):
            failed = True
            if stderr:
                print(stderr[-1600:], file=sys.stderr, flush=True)
    (out / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())

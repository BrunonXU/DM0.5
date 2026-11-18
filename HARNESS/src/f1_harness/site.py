"""Prepare explicit site inputs and supervise auxiliary services, never the motion owner."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time


COMPONENTS = ('owner', 'task', 'plan', 'verify', 'numerics', 'gateway', 'masks')


def _native_spec(controller, infra_root, profile, run, runtime):
    """Pure model constructor, matching the previously validated numerical probe."""
    import numpy as np
    from f1dagger.config import load_config
    from f1dagger.contract import JointMapping, FULL_NAMES
    from f1dagger.robot_io import CollisionGate
    config, machine = load_config(controller, infra_root=infra_root, profile_path=profile)
    urdf = infra_root / 'web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf'
    mapping_path = infra_root / 'configs/model_mapping.json'
    mapping = JointMapping(json.loads(mapping_path.read_text()), machine, urdf)
    preset = machine['default_presets'][1]
    full = np.asarray([preset['joints'][name] for name in FULL_NAMES], dtype=float)
    gate = CollisionGate(config, mapping, run / 'model-build', full)
    spec = dict(gate=gate.plan_worker_spec, urdf=str(urdf), ik=config['ik'],
                position_tolerance_m=.0005, rotation_tolerance_rad=.005, max_iterations=3000)
    spec['gate']['runtime'] = str(runtime / 'numerics-gate')
    return spec, dict(input='profile preset, not live feedback', preset_index=1,
                     joint_names=list(FULL_NAMES), full22=full.tolist(),
                     source_urdf_sha256=hashlib.sha256(urdf.read_bytes()).hexdigest(),
                     source_mapping_sha256=hashlib.sha256(mapping_path.read_bytes()).hexdigest())


def prepare_native(directory, *, bundle, infra_root, profile, control_lock, owner_port, run):
    """Populate only local infrastructure/model inputs; never grant physical admission."""
    directory, bundle, run = (Path(p).resolve() for p in (directory, bundle, run))
    supplied = dict(infra_root=Path(infra_root), profile=Path(profile), control_lock=Path(control_lock))
    if any(not value.is_absolute() for value in supplied.values()):
        raise ValueError('absolute_native_paths_required')
    if type(owner_port) is not int or not 1024 <= owner_port <= 65535:
        raise ValueError('invalid_owner_port')
    infra_root, profile, control_lock = (supplied[k] for k in ('infra_root', 'profile', 'control_lock'))
    source = bundle / 'control/configs/f1_09_hil.json'
    controller = directory / 'controller.local.json'
    numerics_path, launch_path = directory / 'numerics.local.json', directory / 'launch.local.json'
    numerics, launch = (json.loads(p.read_text()) for p in (numerics_path, launch_path))
    if controller.exists() or controller.is_symlink() or numerics.get('spec') is not None:
        raise ValueError('native_configuration_already_filled')
    if launch.get('schema') != 'f1-harness-site-launch-v1' or not Path(launch['runtime']).is_absolute():
        raise ValueError('invalid_existing_launch_draft')
    additions = dict(controller_config=str(controller), infra_root=str(infra_root),
                     profile=str(profile), control_lock=str(control_lock), owner_port=owner_port)
    if any(launch.get(key) is not None and launch[key] != value for key, value in additions.items()):
        raise ValueError('native_launch_binding_conflict')
    if not source.is_file() or not profile.is_file() or not infra_root.is_dir():
        raise ValueError('native_model_sources_unavailable')
    source_bytes = source.read_bytes()
    candidate = json.loads(source_bytes)
    candidate.pop('accepted_release_manifest_sha256', None)
    candidate['ik']['verified'] = False
    candidate['ik']['verification_note'] = 'Harness development candidate; physical and integration admission pending.'
    serialized = (json.dumps(candidate, indent=2) + '\n').encode()
    run.mkdir(parents=True, exist_ok=True)
    candidate_path = run / 'controller.candidate.json'
    with candidate_path.open('xb') as stream:
        stream.write(serialized)
    # Uses CollisionGate only. No F1Owner, RosTransport, output or model API is created.
    spec, model_input = _native_spec(candidate_path, infra_root, profile, run, Path(launch['runtime']))
    old_numerics, old_launch = numerics_path.read_bytes(), launch_path.read_bytes()
    if json.loads(old_numerics) != numerics or json.loads(old_launch) != launch:
        raise ValueError('site_changed_during_native_preparation')
    numerics['spec'] = spec
    launch.update(additions)
    with controller.open('xb') as stream:
        stream.write(serialized)
    numerics_path.write_text(json.dumps(numerics, indent=2) + '\n')
    launch_path.write_text(json.dumps(launch, indent=2) + '\n')
    receipt = dict(schema='f1-harness-native-preparation-v1', directory=str(directory),
                   controller_config=str(controller), model_input=model_input,
                   source_controller=str(source), source_controller_sha256=hashlib.sha256(source_bytes).hexdigest(),
                   controller_sha256=hashlib.sha256(serialized).hexdigest(),
                   profile_sha256=hashlib.sha256(profile.read_bytes()).hexdigest(),
                   numerics_sha256=hashlib.sha256(numerics_path.read_bytes()).hexdigest(),
                   launch_sha256=hashlib.sha256(launch_path.read_bytes()).hexdigest(),
                   numerical_solver_limits=dict(position_tolerance_m=.0005, rotation_tolerance_rad=.005,
                                                max_iterations=3000),
                   physical_accuracy_verified=False, integration_validated=False,
                   motion_authorized=False, robot_actions=0, command_publishers=0,
                   accepted_release_manifest_assigned=False)
    (run / 'native-preparation.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


def prepare_site(directory, *, runtime_root, templates=None):
    """Create a NEW draft; null measurements and admission flags stay unfilled."""
    directory, runtime_root = Path(directory), Path(runtime_root)
    if not runtime_root.is_absolute():
        raise ValueError('absolute_runtime_root_required')
    templates = Path(templates or Path(__file__).resolve().parents[2] / 'configs')
    documents = {name: json.loads((templates / (name + '.example.json')).read_text())
                 for name in COMPONENTS}
    socket_names = {'owner_socket': 'owner', 'planner_socket': 'plan',
                    'verifier_socket': 'verify', 'numerical_socket': 'numerics'}
    def rewrite(value, component):
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            if key in socket_names:
                value[key] = str(runtime_root / (socket_names[key] + '.sock'))
            elif key == 'socket' and component in ('plan', 'verify', 'numerics'):
                value[key] = str(runtime_root / (component + '.sock'))
            elif isinstance(item, dict):
                rewrite(item, component)
    for name, document in documents.items():
        rewrite(document, name)
    documents['gateway']['observation'] = json.loads((templates / 'observation.readonly.json').read_text())
    documents['launch'] = dict(schema='f1-harness-site-launch-v1',
        controller_config=None, infra_root=None, profile=None,
        runtime=str(runtime_root), control_lock=None, owner_port=None)
    directory.mkdir(parents=True, exist_ok=False)
    for name, document in documents.items():
        (directory / (name + '.local.json')).write_text(json.dumps(document, indent=2) + '\n')
    return dict(directory=str(directory.resolve()), draft=True, motion_authorized=False,
                files=sorted(name + '.local.json' for name in documents))


def admission(directory, *, mode):
    """Use the reviewed precision or narrowly bound development contract, never grant START."""
    from .readiness import inspect_site
    if mode not in ('shadow', 'hardware'):
        raise ValueError('invalid_site_mode')
    report = inspect_site(directory)
    blocking = []
    for row in report['components']:
        reasons = list(row['reasons'])
        if mode == 'shadow' and row['component'] == 'owner' and report.get('admission_profile')!='development_coarse':
            reasons = [r for r in reasons if r != 'integration_not_validated']
        if reasons:
            blocking.append(dict(component=row['component'], reasons=reasons))
    if blocking:
        raise ValueError('site_not_ready:' + json.dumps(blocking, separators=(',', ':')))
    return report


def _unfilled(value):
    if value is None or isinstance(value, str) and 'REPLACE_' in value:
        return True
    if isinstance(value, dict):
        return any(_unfilled(v) for v in value.values())
    if isinstance(value, list):
        return any(_unfilled(v) for v in value)
    return False


def service_commands(directory, *, bundle, python, run, role='robot', mode='shadow', numerical_pool=None):
    """Return argv arrays, with no shell or owner command in the service group."""
    directory, bundle, run = (Path(p).resolve() for p in (directory, bundle, run))
    if role == 'robot':
        admission(directory, mode=mode)
        kinds = ('numerics', 'plan', 'verify', 'gateway')
        entry = [os.path.abspath(python), str(bundle / 'native/entry.py')]
    elif role == 'perception':
        config = json.loads((directory / 'masks.local.json').read_text())
        if _unfilled(config):
            raise ValueError('mask_configuration_unfilled')
        kinds = ('masks',)
        # This interpreter belongs to the separate perception environment.
        # Preserve the venv symlink path: resolving it would bypass pyvenv.cfg.
        entry = [os.path.abspath(python), str(bundle / 'native/entry.py')]
    else:
        raise ValueError('invalid_service_role')
    if not Path(entry[0]).is_file() or not Path(entry[1]).is_file():
        raise ValueError('explicit_python_and_bundle_entry_required')
    commands=[dict(name=kind, argv=entry + ['--run', str(run / kind), 'serve', kind,
                                        str(directory / (kind + '.local.json'))]) for kind in kinds]
    if numerical_pool is not None:
        path=Path(numerical_pool)
        if not path.is_absolute() or '..' in path.parts or len(str(path).encode())>=108:
            raise ValueError('invalid_numerical_pool_socket')
        for row in commands:
            if row['name']=='numerics':row['argv']+=['--numerical-pool',str(path)]
    return commands


def owner_arguments(directory, *, mode, web_control=False):
    """Select explicit Harness authorization while retaining the established owner and locks."""
    directory = Path(directory).resolve()
    if web_control and mode!='hardware':raise ValueError('web_control_requires_hardware_owner')
    admission(directory, mode=mode)
    launch = json.loads((directory / 'launch.local.json').read_text())
    if launch.get('schema') != 'f1-harness-site-launch-v1' or _unfilled(launch):
        raise ValueError('launch_configuration_unfilled')
    if type(launch['owner_port']) is not int or not 1024 <= launch['owner_port'] <= 65535:
        raise ValueError('invalid_owner_port')
    for name in ('controller_config', 'infra_root', 'profile', 'runtime', 'control_lock'):
        if not Path(launch[name]).is_absolute():
            raise ValueError('absolute_launch_paths_required')
    argv = ['harness-web' if web_control else 'harness' if mode == 'hardware' else 'harness-shadow',
            '--harness-config', str(directory / 'owner.local.json'),
            '--config', launch['controller_config'], '--infra-root', launch['infra_root'],
            '--profile', launch['profile'], '--runtime', launch['runtime'],
            '--port', str(launch['owner_port']),
            '--task', 'Harness 积木抓取、对孔与放置评测']
    if mode == 'hardware':
        argv.append('--execute')
    return argv, launch['control_lock']


def _safe_environment():
    # Services do not need planner credentials. Do not inherit generic credentials,
    # loader injection or the host development PYTHONPATH into the native snapshot.
    allowed = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'LD_LIBRARY_PATH', 'AMENT_PREFIX_PATH',
               'COLCON_PREFIX_PATH', 'CMAKE_PREFIX_PATH', 'ROS_DISTRO', 'ROS_VERSION',
               'ROS_PYTHON_VERSION', 'ROS_DOMAIN_ID', 'RMW_IMPLEMENTATION',
               'CYCLONEDDS_URI', 'FASTRTPS_DEFAULT_PROFILES_FILE', 'PYTHONPATH')
    result = {key: os.environ[key] for key in allowed if key in os.environ}
    # ROS generated modules are the only external Python paths needed at startup.
    result['PYTHONPATH'] = os.pathsep.join(p for p in result.get('PYTHONPATH', '').split(os.pathsep)
                                         if p.startswith('/opt/ros/'))
    result['PYTHONUNBUFFERED'] = '1'
    return result


def supervise(commands, run, *, startup_timeout=30., stop=None, env=None, parallel=False):
    """Fail together; reap every owned process group. Never kills existing services."""
    import threading
    stop = stop or threading.Event()
    if not 0 < startup_timeout <= 120:
        raise ValueError('bounded_startup_timeout_required')
    run = Path(run)
    run.mkdir(parents=True, exist_ok=False)
    children = []
    receipt = dict(services=[], state='starting', live_geometry_checked=False, motion_authorized=False)
    def wait_ready(process, name, path, deadline):
        while time.monotonic() < deadline and not stop.is_set():
            if process.poll() is not None:
                raise RuntimeError('service_exited_before_ready:' + name)
            if any(p.poll() is not None for p, _ in children):
                raise RuntimeError('dependency_exited_during_startup')
            for line in path.read_text(errors='replace').splitlines():
                try:value=json.loads(line)
                except ValueError:continue
                if isinstance(value,dict) and value.get('ready') is True and value.get('service')==name:
                    return
            stop.wait(.05)
        if not stop.is_set():raise RuntimeError('service_ready_timeout:' + name)
    waiting=[]
    try:
        for command in commands:
            if stop.is_set():
                break
            name = command['name']
            if not name.replace('_', '').isalnum() or any(c['name'] == name for c in receipt['services']):
                raise ValueError('invalid_or_duplicate_service_name')
            path = run / (name + '.log')
            log = path.open('xb')
            try:
                process = subprocess.Popen(command['argv'], stdout=log, stderr=subprocess.STDOUT,
                                           env=env or _safe_environment(), start_new_session=True)
            except BaseException:
                log.close()
                raise
            children.append((process, log))
            receipt['services'].append(dict(name=name, pid=process.pid, argv=command['argv'], log=str(path)))
            deadline = time.monotonic() + startup_timeout
            if parallel:waiting.append((process,name,path,deadline))
            else:wait_ready(process,name,path,deadline)
        for process,name,path,deadline in waiting:
            wait_ready(process,name,path,deadline)
        if not stop.is_set():
            receipt['state'] = 'running'
            (run / 'services.json').write_text(json.dumps(receipt, indent=2) + '\n')
            print(json.dumps(dict(ready=True, services=[s['name'] for s in receipt['services']],
                                  owner_started=False, live_geometry_checked=False,
                                  motion_authorized=False)), flush=True)
        while not stop.wait(.1):
            if any(p.poll() is not None for p, _ in children):
                raise RuntimeError('service_exited_group_stopped')
        receipt['state'] = 'stopped'
    except KeyboardInterrupt:
        receipt['state'] = 'stopped'
    except BaseException as exc:
        receipt.update(state='failed', error=type(exc).__name__, reason=str(exc))
        raise
    finally:
        # SIGINT gives existing serve() finally blocks time to unlink their own sockets.
        for process, _ in reversed(children):
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        for process, log in reversed(children):
            try:
                process.wait(timeout=max(.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
            # Also reap descendants even if the group leader exited before shutdown.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
            log.close()
        for row, (process, _) in zip(receipt['services'], children):
            row['returncode'] = process.returncode
        (run / 'services.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init'); init.add_argument('directory', type=Path)
    init.add_argument('--runtime-root', required=True, type=Path)
    start = sub.add_parser('services'); start.add_argument('directory', type=Path)
    start.add_argument('--bundle', required=True, type=Path)
    start.add_argument('--python', required=True, type=Path)
    start.add_argument('--run', required=True, type=Path)
    start.add_argument('--role', choices=('robot', 'perception'), default='robot')
    start.add_argument('--mode', choices=('shadow', 'hardware'), default='shadow')
    start.add_argument('--print-only', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'init':
        result = prepare_site(args.directory, runtime_root=args.runtime_root)
    else:
        commands = service_commands(args.directory, bundle=args.bundle, python=args.python,
                                    run=args.run, role=args.role, mode=args.mode)
        if args.print_only:
            result = dict(commands=commands, owner_started=False, motion_authorized=False)
        else:
            old = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
            try:
                result = supervise(commands, args.run, parallel=True)
            finally:
                signal.signal(signal.SIGTERM, old)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())

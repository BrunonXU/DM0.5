"""Portable configuration and child-process contracts; no ROS or robot access."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import project_paths
from recording_service import RecordingService
from teleop_service import TeleopService


@pytest.fixture(autouse=True)
def clean_installation_environment(monkeypatch, tmp_path):
    for key in tuple(os.environ):
        if key.startswith(('F1_', 'PICO_', 'ROS_')) or key in ('CYCLONEDDS_URI', 'RMW_IMPLEMENTATION'):
            monkeypatch.delenv(key)
    monkeypatch.setenv('F1_INFRA_HOME', str(tmp_path / 'installation'))
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')


def write_profile(path):
    profile = json.loads((ROOT / 'configs/robot.example.json').read_text())
    profile.update(id='custom-fixture-robot', ip='127.0.0.1',
                   expected_machine_id=Path('/etc/machine-id').read_text().strip(),
                   data_path='${F1_CONSOLE_RUNTIME}/raw_data')
    path.write_text(json.dumps(profile))
    return profile


def child_configuration(environment, cwd=ROOT):
    result = subprocess.run(
        [sys.executable, '-c',
         'import json; from project_paths import read_profile, installation_environment, teleop_config; '
         'print(json.dumps({"profile":read_profile(), "env":installation_environment(), "teleop":teleop_config()}))'],
        cwd=cwd, env=dict(os.environ, **environment), check=True,
        capture_output=True, text=True, timeout=10,
    )
    return json.loads(result.stdout)


def test_runtime_override_derives_one_shared_lock_and_pico_runtime(tmp_path):
    runtime = tmp_path / 'selected runtime'
    values = project_paths.installation_environment(runtime)
    assert values['F1_CONSOLE_RUNTIME'] == str(runtime)
    assert values['F1_CONTROL_LOCK'] == str(runtime / 'control.lock')
    assert values['PICO_UPPER_CONTROL_LOCK'] == values['F1_CONTROL_LOCK']
    assert values['PICO_UPPER_RUNTIME'] == str(runtime / 'pico_upper')


def test_explicit_shared_paths_survive_runtime_override(monkeypatch, tmp_path):
    lock = tmp_path / 'shared' / 'control.lock'
    pico_runtime = tmp_path / 'existing-pico-runtime'
    monkeypatch.setenv('F1_CONTROL_LOCK', str(lock))
    monkeypatch.setenv('PICO_UPPER_CONTROL_LOCK', str(tmp_path / 'wrong.lock'))
    monkeypatch.setenv('PICO_UPPER_RUNTIME', str(pico_runtime))
    values = project_paths.installation_environment(tmp_path / 'new runtime')
    assert values['F1_CONTROL_LOCK'] == values['PICO_UPPER_CONTROL_LOCK'] == str(lock)
    assert values['PICO_UPPER_RUNTIME'] == str(pico_runtime)


def test_recording_gateway_passes_selected_profile_and_shared_paths(monkeypatch, tmp_path):
    profile = tmp_path / 'selected robot.json'
    write_profile(profile)
    runtime = tmp_path / 'custom runtime'
    lock = tmp_path / 'shared.lock'
    monkeypatch.setenv('F1_PROFILE_PATH', str(profile))
    monkeypatch.setenv('F1_CONTROL_LOCK', str(lock))
    monkeypatch.setenv('F1_RECORDING_PYTHON', sys.executable)
    service = RecordingService(runtime, ROOT)
    states = iter(({'mode': 'not_started'}, {'mode': 'idle'}))
    monkeypatch.setattr(service, 'state', lambda: next(states))
    launches = []
    with monkeypatch.context() as scoped:
        def spawn(args, **kwargs):
            launches.append((args, kwargs))
            return SimpleNamespace(poll=lambda: None)
        scoped.setattr('recording_service.subprocess.Popen', spawn)
        assert service.ensure_worker()['mode'] == 'idle'
    args, kwargs = launches[0]
    assert args == ['bash', str(ROOT / 'recording/run.sh')]
    child = child_configuration(kwargs['env'])
    assert child['profile']['id'] == 'custom-fixture-robot'
    assert child['profile']['data_path'] == str(runtime / 'raw_data')
    assert child['env']['F1_CONSOLE_RUNTIME'] == str(runtime)
    assert child['env']['F1_CONTROL_LOCK'] == child['env']['PICO_UPPER_CONTROL_LOCK'] == str(lock)


def test_teleop_systemd_child_gets_selected_profile_and_expanded_paths(monkeypatch, tmp_path):
    profile = tmp_path / 'selected robot.json'
    write_profile(profile)
    runtime = tmp_path / 'runtime with spaces'
    lock = tmp_path / 'shared.lock'
    pico = tmp_path / 'independent pico'
    pico.mkdir()
    launcher = pico / 'start.sh'
    launcher.write_text('echo fixture-only\n')
    root = tmp_path / 'code'
    (root / 'teleop').mkdir(parents=True)
    config = {'upper': {'cwd': '${PICO_UPPER_ROOT}',
                        'start': '${PICO_UPPER_ROOT}/start.sh',
                        'original_stop': '${PICO_UPPER_ROOT}/stop.sh',
                        'start_sha256': hashlib.sha256(launcher.read_bytes()).hexdigest()}}
    (root / 'teleop/config.json').write_text(json.dumps(config))
    monkeypatch.setenv('F1_PROFILE_PATH', str(profile))
    monkeypatch.setenv('F1_CONTROL_LOCK', str(lock))
    monkeypatch.setenv('PICO_UPPER_ROOT', str(pico))
    monkeypatch.setenv('F1_CONSOLE_URL', 'http://127.0.0.1:19555')
    launches = []
    service = TeleopService(runtime, root)
    with monkeypatch.context() as scoped:
        scoped.setattr('teleop_service.subprocess.run',
                       lambda args, **kwargs: launches.append(args) or SimpleNamespace(returncode=0))
        service.prepare('upper', 'fixture-owner')
    args = launches[0]
    child_env = dict(item[len('--setenv='):].split('=', 1)
                     for item in args if item.startswith('--setenv='))
    child = child_configuration(child_env)
    assert child['profile']['id'] == 'custom-fixture-robot'
    assert child['env']['F1_CONSOLE_RUNTIME'] == str(runtime)
    assert child['env']['F1_CONTROL_LOCK'] == child['env']['PICO_UPPER_CONTROL_LOCK'] == str(lock)
    assert child['env']['F1_CONSOLE_URL'] == 'http://127.0.0.1:19555'
    assert service.config['upper']['start'] == str(launcher)
    assert child['teleop']['upper']['start'] == str(launcher)
    assert Path(args[-1]).parent == runtime / 'teleop/sessions'
    assert not (service.current() / 'confirm.json').exists()


class BeforeRobotImports(Exception):
    pass


def test_recording_worker_uses_selected_machine_profile_before_ros(monkeypatch, tmp_path):
    from recording import worker
    profile = tmp_path / 'selected.json'
    write_profile(profile)
    monkeypatch.setenv('F1_PROFILE_PATH', str(profile))
    monkeypatch.setattr(sys, 'argv', ['recording.worker', '--runtime', str(tmp_path / 'runtime')])
    def stop_before_imports(*args):
        raise BeforeRobotImports
    monkeypatch.setattr(worker.fcntl, 'flock', stop_before_imports)
    with pytest.raises(BeforeRobotImports):
        worker.main()


def test_teleop_worker_uses_selected_machine_profile_before_hardware(monkeypatch, tmp_path):
    from teleop import worker
    profile = tmp_path / 'selected.json'
    write_profile(profile)
    monkeypatch.setenv('F1_PROFILE_PATH', str(profile))
    launcher = tmp_path / 'never-run.sh'
    launcher.write_text('exit 99\n')
    session = worker.Session.__new__(worker.Session)
    session.cfg = {'start': str(launcher), 'start_sha256': hashlib.sha256(launcher.read_bytes()).hexdigest()}
    def stop_before_hardware():
        raise BeforeRobotImports
    monkeypatch.setattr(worker, 'check_hardware', stop_before_hardware)
    with pytest.raises(BeforeRobotImports):
        session.run()


@pytest.fixture
def unpacked_source(tmp_path):
    """Only project sources; there is no F1 sibling and no developer PYTHONPATH."""
    root = tmp_path / 'arbitrary unpack 路径' / 'F1-infra'
    root.mkdir(parents=True)
    for source in ROOT.glob('*.py'):
        shutil.copy2(source, root / source.name)
    shutil.copy2(ROOT / 'run.sh', root / 'run.sh')
    for directory in ('robot', 'recording', 'teleop'):
        for source in (ROOT / directory).rglob('*.py'):
            target = root / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for relative in ('configs/robot.example.json', 'teleop/config.json'):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    return root


def test_templates_follow_arbitrary_unpacked_project(unpacked_source, tmp_path):
    profile = unpacked_source / 'configs/robot.json'
    write_profile(profile)
    environment = {'F1_CONSOLE_RUNTIME': str(tmp_path / 'standalone runtime'), 'PYTHONPATH': ''}
    child = child_configuration(environment, cwd=unpacked_source)
    assert child['profile']['id'] == 'custom-fixture-robot'
    assert child['env']['F1_PROFILE_PATH'] == str(profile)
    pico = unpacked_source / 'integrations/pico_upper'
    assert child['teleop']['upper']['cwd'] == str(pico)
    assert child['teleop']['upper']['start'] == str(pico / 'start.sh')
    assert child['teleop']['upper']['original_stop'] == str(pico / 'stop.sh')
    assert not (unpacked_source.parent / 'F1').exists()


def gateway_inspector(tmp_path):
    inspector = tmp_path / 'inspect-python'
    inspector.write_text('#!' + sys.executable + '\n' + '''
import json, os, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(sys.argv[1]).parent))
import server
captured = {}
def app(profile, runtime, control_lock, offline, web_root):
    captured.update(profile=profile, runtime=runtime, control_lock=control_lock, offline=offline,
                    ros_log_dir=os.environ.get('ROS_LOG_DIR'),
                    ros_log_exists=Path(os.environ.get('ROS_LOG_DIR', '/missing-ros-logs')).is_dir())
    return SimpleNamespace(close=lambda: None)
class Server:
    def __init__(self, *args): pass
    def serve_forever(self, **kwargs): pass
    def server_close(self): pass
server.App = app
server.Server = Server
sys.argv = sys.argv[1:]
server.main()
captured['env'] = server.installation_environment()
captured['ros_imported'] = 'rclpy' in sys.modules
print(json.dumps(captured))
''')
    inspector.chmod(0o755)
    return inspector


@pytest.mark.parametrize('explicit_lock', [False, True])
@pytest.mark.parametrize('path_form', ['absolute', 'relative', 'tilde'])
def test_offline_launcher_cli_paths_reach_all_children(unpacked_source, tmp_path, explicit_lock, path_form):
    """Execute the real launcher/main but replace the server before binding sockets."""
    profile = unpacked_source / 'configs/selected.json'
    write_profile(profile)
    inspector = gateway_inspector(tmp_path)
    def argument(path):
        if path_form == 'relative':
            return os.path.relpath(path, tmp_path)
        if path_form == 'tilde':
            return '~/' + os.path.relpath(path, Path.home())
        return str(path)
    runtime = tmp_path / 'CLI runtime'
    lock = tmp_path / 'CLI shared.lock' if explicit_lock else runtime / 'control.lock'
    env = dict(os.environ, F1_CONSOLE_PYTHON=str(inspector), PYTHONPATH='',
               F1_ROS_SETUP='/missing/ros.bash', F1_ROBOT_SETUP='/missing/robot.bash',
               F1_AGV_SETUP='/missing/agv.bash')
    result = subprocess.run(
        ['bash', str(unpacked_source / 'run.sh'), '--offline', '--profile', argument(profile),
         '--runtime', argument(runtime), '--port', '19557'] + (['--control-lock', argument(lock)] if explicit_lock else []),
        cwd=tmp_path, env=env, check=True, capture_output=True, text=True, timeout=10,
    )
    report = json.loads(result.stdout.splitlines()[-1])
    assert report['offline'] and not report['ros_imported']
    assert report['ros_log_dir'] is None and not report['ros_log_exists']
    assert report['profile']['id'] == 'custom-fixture-robot'
    assert report['profile']['data_path'] == str(runtime / 'raw_data')
    assert report['env']['F1_PROFILE_PATH'] == str(profile)
    assert report['runtime'] == report['env']['F1_CONSOLE_RUNTIME'] == str(runtime)
    assert report['control_lock'] == report['env']['F1_CONTROL_LOCK'] == report['env']['PICO_UPPER_CONTROL_LOCK'] == str(lock)
    assert report['env']['PICO_UPPER_RUNTIME'] == str(runtime / 'pico_upper')
    assert report['env']['PICO_UPPER_ROOT'] == str(unpacked_source / 'integrations/pico_upper')
    assert report['env']['F1_CONSOLE_URL'] == 'http://127.0.0.1:19557'


def test_recording_shell_preserves_gateway_environment(tmp_path):
    profile = tmp_path / 'selected robot.json'
    write_profile(profile)
    runtime = tmp_path / 'recording runtime'
    lock = tmp_path / 'shared.lock'
    ros_setup = tmp_path / 'ros-fixture.bash'
    robot_setup = tmp_path / 'robot-fixture.bash'
    ros_setup.write_text('export TEST_ROS_SETUP_READ=yes\n')
    robot_setup.write_text('export TEST_ROBOT_SETUP_READ=yes\n')
    ignored_env = tmp_path / 'must-not-reload.env'
    ignored_env.write_text('exit 88\n')
    inspector = tmp_path / 'recording-python'
    inspector.write_text('#!' + sys.executable + '\n' +
                         'import json, os, sys; from project_paths import read_profile, installation_environment; '
                         'print(json.dumps({"argv":sys.argv[1:], "profile":read_profile(), '
                         '"env":installation_environment(), "ros":os.environ["TEST_ROS_SETUP_READ"], '
                         '"robot":os.environ["TEST_ROBOT_SETUP_READ"]}))\n')
    inspector.chmod(0o755)
    env = dict(os.environ, F1_PROFILE_PATH=str(profile), F1_CONSOLE_RUNTIME=str(runtime),
               F1_CONTROL_LOCK=str(lock), F1_RECORDING_PYTHON=str(inspector),
               F1_ROS_SETUP=str(ros_setup), F1_ROBOT_SETUP=str(robot_setup),
               F1_ENV_LOADED='1', F1_INSTALLATION_ENV=str(ignored_env), PYTHONPATH='')
    result = subprocess.run(['bash', str(ROOT / 'recording/run.sh'), '--check'],
                            cwd=tmp_path, env=env, check=True, capture_output=True,
                            text=True, timeout=10)
    report = json.loads(result.stdout)
    assert report['argv'] == ['-m', 'recording.worker', '--runtime', str(runtime), '--check']
    assert report['profile']['id'] == 'custom-fixture-robot'
    assert report['profile']['data_path'] == str(runtime / 'raw_data')
    assert report['env']['F1_CONTROL_LOCK'] == report['env']['PICO_UPPER_CONTROL_LOCK'] == str(lock)
    assert report['env']['PICO_UPPER_RUNTIME'] == str(runtime / 'pico_upper')
    assert report['ros'] == report['robot'] == 'yes'


def test_online_logs_use_final_cli_runtime_and_preserve_explicit_shared_lock(unpacked_source, tmp_path):
    profile = unpacked_source / 'configs/selected.json'
    write_profile(profile)
    inspector = gateway_inspector(tmp_path)
    setup = tmp_path / 'empty-setup.bash'
    setup.write_text('# Fixture: no ROS imports or processes.\n')
    runtime = tmp_path / 'CLI-selected runtime'
    lock = tmp_path / 'existing shared.lock'
    old_runtime = tmp_path / 'old default runtime'
    env = dict(os.environ, F1_CONSOLE_PYTHON=str(inspector), PYTHONPATH='',
               F1_ROS_SETUP=str(setup), F1_ROBOT_SETUP=str(setup), F1_AGV_SETUP=str(setup),
               F1_CONSOLE_RUNTIME=str(old_runtime),
               F1_CONTROL_LOCK='~/' + os.path.relpath(lock, Path.home()))
    result = subprocess.run(
        ['bash', str(unpacked_source / 'run.sh'), '--profile', str(profile),
         '--runtime', os.path.relpath(runtime, tmp_path)],
        cwd=tmp_path, env=env, check=True, capture_output=True, text=True, timeout=10,
    )
    report = json.loads(result.stdout.splitlines()[-1])
    assert not report['offline'] and not report['ros_imported']
    assert report['ros_log_dir'] == str(runtime / 'ros_logs')
    assert report['ros_log_exists']  # Present before App would initialize ROS.
    assert not old_runtime.exists()
    assert report['runtime'] == report['env']['F1_CONSOLE_RUNTIME'] == str(runtime)
    assert report['control_lock'] == report['env']['PICO_UPPER_CONTROL_LOCK'] == str(lock)


def test_unresolved_setup_does_not_source_partial_environment(unpacked_source, tmp_path):
    marker = tmp_path / 'must-not-load-ros'
    setup = tmp_path / 'ros.bash'
    setup.write_text(f'touch "{marker}"\n')
    env = dict(os.environ, F1_ROS_SETUP=str(setup), F1_ROBOT_SETUP='',
               F1_AGV_SETUP=str(setup), F1_INSTALLATION_ENV=str(tmp_path / 'absent.env'))
    result = subprocess.run(['bash', str(unpacked_source / 'run.sh')], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert 'setup-report.json' in result.stderr
    assert not marker.exists()


def test_unresolved_dds_does_not_fall_back_to_legacy_config(unpacked_source, tmp_path):
    marker = tmp_path / 'must-not-load-ros'
    setup = tmp_path / 'ros.bash'
    setup.write_text(f'touch "{marker}"\n')
    env = dict(os.environ, F1_ROS_SETUP=str(setup), F1_ROBOT_SETUP=str(setup),
               F1_AGV_SETUP=str(setup), F1_CYCLONEDDS_URI='',
               F1_INSTALLATION_ENV=str(tmp_path / 'absent.env'))
    result = subprocess.run(['bash', str(unpacked_source / 'run.sh')], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert 'DDS' in result.stderr
    assert not marker.exists()

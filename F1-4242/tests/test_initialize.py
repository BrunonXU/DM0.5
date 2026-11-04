"""First-install preparation must never borrow another robot's control approval."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

MODULE = Path(__file__).resolve().parents[1] / 'deploy/initialize.py'
spec = importlib.util.spec_from_file_location('f1_initialize', MODULE)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


@pytest.fixture
def inventory(tmp_path):
    return {'schema': setup.SCHEMA, 'account': {'home': str(tmp_path / 'home'), 'username': 'robot'},
            'machine_id': '00000000000000000000000000000000', 'architecture': 'aarch64',
            'addresses': [{'address': '192.0.2.3', 'interface': 'wifi'}, {'address': '192.0.2.2', 'interface': 'dds'}],
            'ros_setup': ['/opt/ros/jazzy/setup.bash'], 'f1_setup': ['/home/robot/workspace/f1/install/setup.bash'],
            'agv_setup': ['/home/robot/workspace/agv/install/setup.bash'],
            'dds': {'domain_id': '0', 'config': '/etc/ros/dds.xml', 'rmw': 'rmw_cyclonedds_cpp'},
            'lock_candidates': ['/home/robot/existing/shared-control.lock'],
            'optional_paths': {}, 'disk': {'free_bytes': 1024**3, 'path': '/home/robot'}}


def prepare(tmp_path, inventory, **kwargs):
    args = dict(name='F1-09', ip='192.0.2.3', data_dir=tmp_path / 'data', modules=[], output_dir=tmp_path / 'install', simulation=True)
    args.update(kwargs)
    return setup.prepare(inventory, **args)


@pytest.mark.parametrize('value', ['', ' ', 'x\ny', 'x\x00y', 'x\x7fy', 'a' * 81])
def test_invalid_name(value):
    with pytest.raises(setup.SetupError):
        setup.validate_name(value)


@pytest.mark.parametrize('value', ['0.0.0.0', '255.255.255.255', '224.0.0.1', '192.168.1.300', 'example.com', '::1'])
def test_invalid_ip(value):
    with pytest.raises(setup.SetupError):
        setup.validate_ip(value)


@pytest.mark.parametrize('value', ['', '/', 'relative/path', '/tmp/../etc', '/tmp/bad\npath'])
def test_invalid_data_directory(value):
    with pytest.raises(setup.SetupError):
        setup.absolute_path(value)


def test_data_tilde_expands_to_robot_account():
    assert setup.absolute_path('~/F1-data', '/home/user') == Path('/home/user/F1-data')


def test_name_and_access_ip_do_not_change_control_identity(tmp_path, inventory):
    first, report = prepare(tmp_path, inventory)
    other, _ = prepare(tmp_path, inventory, name='实验室小一', ip='192.0.2.14')
    assert first['configs/robot.json'] == other['configs/robot.json']
    robot = json.loads(first['configs/robot.json'])
    assert robot['id'] == robot['robot_id'] == 'machine-000000000000'
    assert robot['expected_machine_id'] == inventory['machine_id']
    assert robot['default_presets'] == []
    assert robot['startup_script'] == robot['ec_startup_script'] == ''
    for key, value in robot.items():
        if key.endswith(('_enabled', '_verified')):
            assert value is False
    assert report['simulation'] is True
    assert report['ros_ready'] is False
    assert report['ros_files_found'] is True
    assert report['motion_enabled'] is False
    assert report['control_lock']['path'] == inventory['lock_candidates'][0]
    assert report['effects'] == {key: False for key in report['effects']}
    assert json.loads(first['configs/site.json']) == {'schema': 'f1_site_v1', 'label': 'F1-09', 'ip': '192.0.2.3'}
    assert first['configs/installation.env'].count('F1_CONSOLE_PORT=4242') == 1
    assert 'F1_LXL_ROOT=' in first['configs/installation.env']


def test_shell_quotes_preserve_literal_paths(tmp_path, inventory):
    marker = tmp_path / 'INJECTION_SENTINEL'
    tricky = tmp_path / ('installation \' $(touch ' + marker.name + ') `echo bad` ; literal')
    files, _ = prepare(tmp_path, inventory, output_dir=tricky, name='$(touch should-not-exist)')
    setup.write_new_installation(tricky, files)
    # A real Bash source exercise verifies literal quoting, not an implementation mirror.
    program = 'source "$1"; printf "%s\\n" "$F1_INFRA_HOME" "$F1_CONTROL_LOCK"'
    result = subprocess.run(['bash', '-c', program, '_', str(tricky / 'configs/installation.env')], cwd=tmp_path, text=True, capture_output=True, check=True)
    assert result.stdout.splitlines() == [str(tricky), inventory['lock_candidates'][0]]
    assert not marker.exists()
    assert not (tmp_path / 'should-not-exist').exists()


def test_existing_configuration_blocks_all_writes(tmp_path, inventory):
    root = tmp_path / 'install'
    (root / 'configs').mkdir(parents=True)
    existing = root / 'configs/robot.json'
    existing.write_text('{"motion_enabled": true}')
    files, _ = prepare(tmp_path, inventory)
    with pytest.raises(setup.SetupError, match='已有配置'):
        setup.write_new_installation(root, files)
    assert existing.read_text() == '{"motion_enabled": true}'
    assert not (root / 'configs/installation.env').exists()
    assert not (root / 'runtime').exists()


def test_identical_rerun_is_noop(tmp_path, inventory):
    root = tmp_path / 'install'
    files, _ = prepare(tmp_path, inventory)
    assert len(setup.write_new_installation(root, files)) == 5
    before = {name: (root / name).stat().st_mtime_ns for name in files}
    assert setup.write_new_installation(root, files) == []
    assert before == {name: (root / name).stat().st_mtime_ns for name in files}


@pytest.mark.parametrize('leaf', ['configs', 'runtime'])
def test_output_symlink_rejected(tmp_path, inventory, leaf):
    outside = tmp_path / 'outside'
    outside.mkdir()
    root = tmp_path / 'install'
    root.mkdir()
    (root / leaf).symlink_to(outside, target_is_directory=True)
    files, _ = prepare(tmp_path, inventory)
    with pytest.raises(setup.SetupError, match='符号链接'):
        setup.write_new_installation(root, files)
    assert not list(outside.iterdir())


def test_data_symlink_rejected_live(tmp_path, inventory):
    link = tmp_path / 'data'
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(setup.SetupError, match='符号链接'):
        prepare(tmp_path, inventory, simulation=False)


def test_write_failure_rolls_back_only_new_files(tmp_path, inventory, monkeypatch):
    root = tmp_path / 'install'
    root.mkdir()
    untouched = root / 'user-note.txt'
    untouched.write_text('keep me')
    files, _ = prepare(tmp_path, inventory)
    original = setup.os.open
    def fail_second(path, *args, **kwargs):
        if str(path).endswith('robot.json'):
            raise OSError('simulated disk error')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(setup.os, 'open', fail_second)
    with pytest.raises(OSError, match='disk error'):
        setup.write_new_installation(root, files)
    assert list(root.iterdir()) == [untouched]
    assert untouched.read_text() == 'keep me'


def test_multi_ip_requires_explicit_choice(tmp_path, inventory, monkeypatch):
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    args = argparse.Namespace(name='F1-09', ip=None, data_dir=str(tmp_path / 'data'), modules='none')
    with pytest.raises(setup.SetupError, match='--ip'):
        setup.choose_inputs(args, inventory)
    args.ip = '192.0.2.3'
    assert setup.choose_inputs(args, inventory)[1] == '192.0.2.3'


def test_multi_lock_prevents_generation(tmp_path, inventory):
    inventory['lock_candidates'].append('/different/control.lock')
    with pytest.raises(setup.SetupError, match='多个共享控制锁'):
        prepare(tmp_path, inventory)
    assert not (tmp_path / 'install').exists()


def test_unresolved_existing_lock_prevents_generation(tmp_path, inventory):
    inventory['unresolved_lock_sources'] = ['/robot/existing/installation.env']
    with pytest.raises(setup.SetupError, match='共享锁无法静态解析'):
        prepare(tmp_path, inventory)


def test_optional_modules_only_prepare_requested_config(tmp_path, inventory):
    base, _ = prepare(tmp_path, inventory)
    pico, p_report = prepare(tmp_path, inventory, modules=['pico'])
    dagger, d_report = prepare(tmp_path, inventory, modules=['dagger'])
    assert 'runtime/recording.json' not in base
    assert 'runtime/recording.json' not in dagger
    recording = json.loads(pico['runtime/recording.json'])
    assert recording['fps'] == 30.0
    assert recording['recording_format'] == 'f1_capture_v2'
    assert p_report['module_status'] == {'pico': 'pending_integration'}
    assert d_report['module_status'] == {'dagger': 'pending_integration'}
    storage = json.loads(base['runtime/storage.json'])
    assert storage['legacy_roots'] == {}
    assert storage['root'] == str(tmp_path / 'data')
    assert json.loads(base['configs/robot.json'])['data_path'] != storage['root']


def test_missing_ros_keeps_explicit_pending(tmp_path, inventory):
    inventory['f1_setup'] = []
    inventory['agv_setup'] = ['/one/setup.bash', '/two/setup.bash']
    files, report = prepare(tmp_path, inventory)
    assert report['web_configured'] is True
    assert report['ros_files_found'] is False
    assert report['ros_ready'] is False
    assert {'f1_setup', 'agv_setup'} <= {item['code'] for item in report['pending']}
    assert "F1_ROBOT_SETUP=''" in files['configs/installation.env']


def test_static_env_never_evaluates_shell(tmp_path):
    marker = tmp_path / 'executed'
    path = tmp_path / 'installation.env'
    path.write_text(f'export F1_CONTROL_LOCK="$(touch {marker})"\nexport PICO_UPPER_CONTROL_LOCK=/existing/control.lock\n')
    values, problems = setup.static_installation(path, tmp_path)
    assert values['PICO_UPPER_CONTROL_LOCK'] == '/existing/control.lock'
    assert 'F1_CONTROL_LOCK' not in values
    assert problems == [str(path)]
    assert not marker.exists()


def test_cli_inventory_marks_simulation_and_does_not_make_data(tmp_path, inventory):
    path = tmp_path / 'inventory.json'
    path.write_text(json.dumps(inventory))
    root = tmp_path / 'install'
    data = tmp_path / 'new-data'
    result = subprocess.run([sys.executable, str(MODULE), '--inventory', str(path), '--name', 'F1-09', '--ip', '192.0.2.3', '--data-dir', str(data), '--modules', 'pico,dagger', '--output-dir', str(root)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert '模拟' in result.stdout
    report = json.loads((root / 'runtime/setup-report.json').read_text())
    assert report['simulation'] is True
    assert report['modules'] == ['dagger', 'pico']
    assert not data.exists()


def test_rerun_does_not_conflict_with_changing_disk_sample(tmp_path, inventory):
    root = tmp_path / 'install'
    files, _ = prepare(tmp_path, inventory)
    setup.write_new_installation(root, files)
    original = (root / 'runtime/setup-report.json').read_bytes()
    inventory['disk']['free_bytes'] -= 4096
    files, _ = prepare(tmp_path, inventory)
    assert setup.write_new_installation(root, files) == []
    assert (root / 'runtime/setup-report.json').read_bytes() == original


def test_live_probe_reuses_running_environment_without_sourcing(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    setup_file = home / 'workspace/f1/install/setup.bash'
    setup_file.parent.mkdir(parents=True)
    setup_file.write_text('exit 99 # must never execute\n')
    installation = home / 'installation.env'
    installation.write_text('export F1_CONTROL_LOCK="' + str(home / 'shared/control.lock') + '"\n')
    monkeypatch.setattr(setup.Path, 'home', classmethod(lambda cls: home))
    for key in ('F1_CONTROL_LOCK', 'PICO_UPPER_CONTROL_LOCK', 'F1_ROS_SETUP', 'F1_ROBOT_SETUP', 'F1_AGV_SETUP', 'F1_CYCLONEDDS_URI', 'CYCLONEDDS_URI', 'ROS_DOMAIN_ID', 'RMW_IMPLEMENTATION', 'F1_INSTALLATION_ENV'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(setup, 'process_environments', lambda: [{
        'F1_INFRA_HOME': str(home / 'old-install'), 'F1_CONSOLE_RUNTIME': str(home / 'runtime'),
        'F1_CONTROL_LOCK': str(home / 'shared/control.lock'), 'F1_INSTALLATION_ENV': str(installation),
        'F1_ROBOT_SETUP': str(setup_file), 'F1_CYCLONEDDS_URI': '/etc/ros/wired.xml',
        'ROS_DOMAIN_ID': '13', 'RMW_IMPLEMENTATION': 'rmw_cyclonedds_cpp'}])
    def no_ip_command(*args, **kwargs):
        raise FileNotFoundError('ip unavailable')
    monkeypatch.setattr(setup.subprocess, 'run', no_ip_command)
    result = setup.probe(tmp_path / 'fresh-install')
    assert result['lock_candidates'] == [str(home / 'shared/control.lock')]
    assert result['f1_setup'] == [str(setup_file)]
    assert result['dds']['config'] == '/etc/ros/wired.xml'
    assert result['dds']['domain_id'] == '13'
    assert result['addresses'] == []
    assert not (tmp_path / 'fresh-install').exists()


def test_invalid_modules_and_machine_id_fail_closed(tmp_path, inventory):
    for value in ('', 'none,pico', 'mp4', 'pico;bash'):
        with pytest.raises(setup.SetupError):
            setup.parse_modules(value)
    inventory['machine_id'] = ''
    with pytest.raises(setup.SetupError, match='machine-id'):
        prepare(tmp_path, inventory)

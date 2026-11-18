import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from f1_harness.site import prepare_site, prepare_native, admission, supervise, service_commands, owner_arguments, _safe_environment


ROOT = Path(__file__).resolve().parents[1]


def test_parallel_services_start_without_serial_readiness_dependency(tmp_path):
    flag=tmp_path/'second-started'
    first=('import pathlib,time,json\n'
           f'flag=pathlib.Path({str(flag)!r})\n'
           'while not flag.exists():time.sleep(.01)\n'
           'print(json.dumps(dict(ready=True,service="first")),flush=True)\n'
           'time.sleep(10)\n')
    second=(f'import pathlib,time,json; pathlib.Path({str(flag)!r}).touch(); '
            'print(json.dumps(dict(ready=True,service="second")),flush=True); time.sleep(10)')
    stop=threading.Event()
    errors=[]
    def run():
        try:supervise([dict(name='first',argv=[sys.executable,'-c',first]),
                       dict(name='second',argv=[sys.executable,'-c',second])],
                      tmp_path/'run',stop=stop,parallel=True,startup_timeout=2)
        except BaseException as exc:errors.append(exc)
    thread=threading.Thread(target=run);thread.start()
    try:
        deadline=time.monotonic()+3
        while not (tmp_path/'run/services.json').exists() and time.monotonic()<deadline:
            time.sleep(.01)
        assert (tmp_path/'run/services.json').exists() and not errors
    finally:
        stop.set();thread.join(timeout=5)
    assert not thread.is_alive()


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_site_init_keeps_measurements_missing_and_socket_bindings(tmp_path):
    destination = tmp_path / 'site'
    result = prepare_site(destination, runtime_root='/task/runtime')
    assert result['draft'] and not result['motion_authorized']
    configs = {p.stem.split('.')[0]: json.loads(p.read_text()) for p in destination.glob('*.json')}
    assert configs['owner']['planner_socket'] == configs['plan']['socket'] == '/task/runtime/plan.sock'
    assert configs['gateway']['scene']['owner_socket'] == configs['owner']['owner_socket']
    assert configs['owner']['calibration_bundle'] is None
    assert configs['plan']['fixture']['gripper_tolerance'] is None
    assert configs['gateway']['observation']['source_clock_verified'] is False
    assert configs['launch']['controller_config'] is None
    with pytest.raises(ValueError, match='site_not_ready'):
        admission(destination, mode='shadow')
    with pytest.raises(FileExistsError):
        prepare_site(destination, runtime_root='/task/runtime')


def test_shadow_exception_is_only_unverified_integration(monkeypatch):
    report = dict(components=[dict(component='owner', reasons=['integration_not_validated'])])
    monkeypatch.setattr('f1_harness.readiness.inspect_site', lambda _: report)
    assert admission('.', mode='shadow') == report
    with pytest.raises(ValueError, match='integration_not_validated'):
        admission('.', mode='hardware')
    report['components'][0]['reasons'].append('calibration_bundle_unavailable_here')
    with pytest.raises(ValueError, match='calibration_bundle_unavailable_here'):
        admission('.', mode='shadow')


def test_commands_preserve_virtualenv_interpreter_path(tmp_path, monkeypatch):
    monkeypatch.setattr('f1_harness.site.admission', lambda *a, **k: {})
    bundle = tmp_path / 'bundle'; (bundle / 'native').mkdir(parents=True)
    (bundle / 'native/entry.py').write_text('')
    interpreter = tmp_path / 'venv/bin/python'; interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    commands = service_commands(tmp_path, bundle=bundle, python=interpreter, run=tmp_path / 'run')
    assert all(command['argv'][0] == str(interpreter) for command in commands)
    assert [command['name'] for command in commands] == ['numerics', 'plan', 'verify', 'gateway']
    assert all('owner' not in command['argv'] for command in commands)


def test_owner_wrapper_preserves_mode_and_original_control_lock(tmp_path, monkeypatch):
    monkeypatch.setattr('f1_harness.site.admission', lambda *a, **k: {})
    config = dict(schema='f1-harness-site-launch-v1',controller_config='/site/controller.json',
                  infra_root='/infra',profile='/site/profile.json',runtime='/runtime',
                  control_lock='/existing/common.lock',owner_port=7906)
    path = tmp_path / 'launch.local.json'; path.write_text(json.dumps(config))
    shadow, lock = owner_arguments(tmp_path, mode='shadow')
    assert shadow[0] == 'harness-shadow' and '--execute' not in shadow
    assert lock == '/existing/common.lock'
    hardware, _ = owner_arguments(tmp_path, mode='hardware')
    assert hardware[0] == 'harness' and hardware[-1] == '--execute'
    assert '--rtc' not in hardware and '--rlt-hil' not in hardware
    web, web_lock = owner_arguments(tmp_path, mode='hardware', web_control=True)
    assert web[0] == 'harness-web' and web[-1] == '--execute' and web_lock == lock
    with pytest.raises(ValueError, match='web_control_requires_hardware'):
        owner_arguments(tmp_path, mode='shadow', web_control=True)
    config['control_lock'] = None; path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match='launch_configuration_unfilled'):
        owner_arguments(tmp_path, mode='hardware')


def _native_fixture(tmp_path, monkeypatch):
    directory = tmp_path / 'site'
    prepare_site(directory, runtime_root=str(tmp_path / 'runtime'))
    bundle = tmp_path / 'bundle'; (bundle / 'control/configs').mkdir(parents=True)
    (bundle / 'control/configs/f1_09_hil.json').write_text(json.dumps(dict(
        ik=dict(verified=True), accepted_release_manifest_sha256='old-production-identity')))
    profile = tmp_path / 'profile.json'; profile.write_text('{}')
    infra = tmp_path / 'infra'; infra.mkdir()
    def build(controller, infra_root, profile_path, run, runtime):
        config = json.loads(controller.read_text())
        assert config['ik']['verified'] is False
        assert 'accepted_release_manifest_sha256' not in config
        return dict(ik=config['ik'], gate={'fixture': True}, position_tolerance_m=.0005,
                    rotation_tolerance_rad=.005, max_iterations=3000), dict(input='fixture preset, not live feedback')
    monkeypatch.setattr('f1_harness.site._native_spec', build)
    return directory, dict(bundle=bundle, infra_root=infra, profile=profile,
                          control_lock=tmp_path / 'common.lock', owner_port=7906, run=tmp_path / 'receipt')


def test_native_preparation_fills_only_static_inputs_without_admission(tmp_path, monkeypatch):
    directory, args = _native_fixture(tmp_path, monkeypatch)
    owner_before = (directory / 'owner.local.json').read_bytes()
    geometry_before = (directory / 'gateway.local.json').read_bytes()
    receipt = prepare_native(directory, **args)
    controller = json.loads((directory / 'controller.local.json').read_text())
    numerics = json.loads((directory / 'numerics.local.json').read_text())
    launch = json.loads((directory / 'launch.local.json').read_text())
    assert numerics['spec']['max_iterations'] == 3000 and launch['owner_port'] == 7906
    assert 'accepted_release_manifest_sha256' not in controller and controller['ik']['verified'] is False
    assert not receipt['physical_accuracy_verified'] and not receipt['motion_authorized']
    assert receipt['robot_actions'] == receipt['command_publishers'] == 0
    assert (directory / 'owner.local.json').read_bytes() == owner_before
    assert (directory / 'gateway.local.json').read_bytes() == geometry_before
    with pytest.raises(ValueError, match='native_configuration_already_filled'):
        prepare_native(directory, **args)


def test_native_preparation_cannot_overwrite_filled_numerics(tmp_path, monkeypatch):
    directory, args = _native_fixture(tmp_path, monkeypatch)
    path = directory / 'numerics.local.json'; value = json.loads(path.read_text()); value['spec'] = {'existing': True}
    path.write_text(json.dumps(value)); before = path.read_bytes()
    with pytest.raises(ValueError, match='native_configuration_already_filled'):
        prepare_native(directory, **args)
    assert path.read_bytes() == before and not (directory / 'controller.local.json').exists()


def test_native_preparation_rejects_existing_conflicting_launch(tmp_path, monkeypatch):
    directory, args = _native_fixture(tmp_path, monkeypatch)
    path = directory / 'launch.local.json'; value = json.loads(path.read_text()); value['control_lock'] = '/other/lock'
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='native_launch_binding_conflict'):
        prepare_native(directory, **args)


def service(name, *, exit_after=None):
    code = 'import json,time,signal\n'
    code += f'print(json.dumps(dict(ready=True,service={name!r})),flush=True)\n'
    code += 'time.sleep(60)\n' if exit_after is None else f'time.sleep({exit_after})\n'
    return dict(name=name, argv=[sys.executable, '-c', code])


def assert_reaped(receipt):
    for row in receipt['services']:
        with pytest.raises(ProcessLookupError):
            os.kill(row['pid'], 0)
        assert row['returncode'] is not None


def test_service_stop_reaps_all_owned_processes(tmp_path):
    stop = threading.Event()
    timer = threading.Timer(.35, stop.set)
    timer.start()
    try:
        receipt = supervise([service('first'), service('second')], tmp_path / 'run', stop=stop)
    finally:
        timer.cancel()
    assert receipt['state'] == 'stopped'
    assert len(receipt['services']) == 2
    assert_reaped(receipt)


def test_dependency_exit_stops_group(tmp_path):
    run = tmp_path / 'run'
    with pytest.raises(RuntimeError, match='service_exited'):
        supervise([service('first'), service('second', exit_after=.2)], run)
    receipt = json.loads((run / 'services.json').read_text())
    assert receipt['state'] == 'failed'
    assert_reaped(receipt)


def test_startup_failure_reaps_started_dependency(tmp_path):
    commands = [service('first'), dict(name='failed', argv=[sys.executable, '-c', 'raise SystemExit(9)'])]
    run = tmp_path / 'run'
    with pytest.raises(RuntimeError, match='service_exited_before_ready'):
        supervise(commands, run)
    assert_reaped(json.loads((run / 'services.json').read_text()))


def test_startup_timeout_is_bounded_and_reaps_child(tmp_path):
    run = tmp_path / 'run'
    commands = [dict(name='silent', argv=[sys.executable, '-c', 'import time;time.sleep(60)'])]
    start = time.monotonic()
    with pytest.raises(RuntimeError, match='service_ready_timeout'):
        supervise(commands, run, startup_timeout=.15)
    assert time.monotonic() - start < 3
    assert_reaped(json.loads((run / 'services.json').read_text()))


def test_auxiliary_environment_excludes_api_and_host_injection(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-only')
    monkeypatch.setenv('PYTHONPATH', '/old/production/src:/opt/ros/jazzy/lib/python3.12/site-packages')
    monkeypatch.setenv('LD_PRELOAD', '/wrong/library')
    environment = _safe_environment()
    assert 'OPENAI_API_KEY' not in environment and 'LD_PRELOAD' not in environment
    assert environment['PYTHONPATH'].startswith('/opt/ros/') and '/old/' not in environment['PYTHONPATH']


def test_service_graceful_shutdown_removes_socket_and_allows_restart(tmp_path):
    socket_path = tmp_path / 'owned.sock'
    code = ('import socket,os,json,time\n'
            f'p={str(socket_path)!r}\n'
            's=socket.socket(socket.AF_UNIX);s.bind(p);s.listen(1)\n'
            'print(json.dumps(dict(ready=True,service="socket_service")),flush=True)\n'
            'try:\n while True:time.sleep(.1)\n'
            'except KeyboardInterrupt:pass\n'
            'finally:s.close();os.unlink(p)\n')
    for index in range(2):
        stop = threading.Event(); timer = threading.Timer(.25, stop.set); timer.start()
        try:
            receipt = supervise([dict(name='socket_service', argv=[sys.executable, '-c', code])],
                                tmp_path / str(index), stop=stop)
        finally:
            timer.cancel()
        assert receipt['state'] == 'stopped' and not socket_path.exists()


def test_bundle_owns_control_and_imports_without_old_source(tmp_path):
    packager = load_script('package_native')
    installer = load_script('install_native_bundle')
    wheels = tmp_path / 'wheels'; wheels.mkdir()
    # Extraction/import test only. This sentinel must never be passed to pip.
    (wheels / 'fixture.whl').write_bytes(b'test fixture')
    result = packager.build_bundle(tmp_path / 'package', wheels)
    destination = tmp_path / 'isolated'
    manifest = installer.extract_verified(result['archive'], destination)
    assert manifest['control_source'] == 'bundled'
    assert 'control/src/f1dagger/harness_runtime.py' in manifest['files']
    assert 'control/src/dagger_core/control_timing.py' in manifest['files']
    assert not any('API.MD' in name or '.local.' in name for name in manifest['files'])
    code = ('import sys,json;from pathlib import Path;root=Path(sys.argv[1]);'
            'sys.path[:0]=[str(root/"src"),str(root/"control/src"),str(root/"upstream/RPent")];'
            'import f1dagger.cli,dagger_core;from f1dagger.identity import runtime_identity;'
            'identity=runtime_identity({"_config_sha256":"fixture-only"},powered=False);'
            'print(json.dumps(dict(source=f1dagger.cli.__file__,identity=identity)))')
    child = subprocess.run([sys.executable, '-c', code, str(destination)], capture_output=True, text=True,
                           cwd=tmp_path, env=_safe_environment(), timeout=20, check=True)
    value = json.loads(child.stdout)
    assert str(destination / 'control/src') in value['source']
    assert value['identity']['release_manifest_sha256'] == manifest['control_manifest_sha256']
    help_result = subprocess.run([sys.executable, str(destination / 'native/entry.py'), '--help'],
                                cwd=tmp_path, capture_output=True, text=True, timeout=10, check=True)
    assert 'owner' in help_result.stdout
    (destination / 'src/f1_harness/native.py').write_text('# changed after installation\n')
    changed = subprocess.run([sys.executable, str(destination / 'native/entry.py'), '--help'],
                             cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert changed.returncode != 0 and 'bundle_source_integrity_failure' in changed.stderr


def test_install_rejects_undeclared_payload_before_extraction(tmp_path):
    from io import BytesIO
    import hashlib
    import tarfile
    installer = load_script('install_native_bundle')
    hashes = {'declared.py': hashlib.sha256(b'').hexdigest()}
    manifest = dict(files=hashes, bundle_id=hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest())
    archive = tmp_path / 'bad.tar'
    with tarfile.open(archive, 'w') as tar:
        for name, data in {'manifest.json': json.dumps(manifest).encode(), 'declared.py': b'',
                           'undeclared.py': b'payload'}.items():
            member = tarfile.TarInfo(name); member.size = len(data); tar.addfile(member, BytesIO(data))
    destination = tmp_path / 'not_created'
    with pytest.raises(ValueError, match='undeclared_archive_member'):
        installer.extract_verified(archive, destination)
    assert not destination.exists()

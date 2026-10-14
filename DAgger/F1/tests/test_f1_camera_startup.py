"""Camera-only launch isolation and transactional persistent profile updates."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def profile_install(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('camera_install', ROOT/'scripts/62_f1_camera_startup_profile.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'BRUNO_ROOT', tmp_path)
    release = tmp_path/'release'; startup = release/'startup'; startup.mkdir(parents=True)
    original = tmp_path/'vendor-start.sh'; original.write_text('original unchanged startup\n')
    for relative, content in {'scripts/60_f1_camera_internal.sh': 'camera wrapper',
                             'configs/f1_camera_fastdds_internal.xml': 'camera XML',
                             'startup/start_all.sh': 'reviewed startup', 'startup/start_all.diff': 'reviewed diff'}.items():
        p = release/relative; p.parent.mkdir(exist_ok=True); p.write_text(content)
    proposal = {'startup_script': str(startup/'start_all.sh'),
                'startup_script_sha256': module.digest((startup/'start_all.sh').read_bytes())}
    (startup/'profile_patch.json').write_text(json.dumps(proposal))
    (startup/'manifest.json').write_text(json.dumps({
        'camera_wrapper': str(release/'scripts/60_f1_camera_internal.sh'),
        'source': {'robot_path': str(original), 'sha256': module.digest(original.read_bytes())}}))
    files = {str(f.relative_to(release)): module.digest(f.read_bytes()) for f in release.rglob('*') if f.is_file()}
    (release/'F1_RELEASE_MANIFEST.json').write_text(json.dumps({'schema': 'f1_dagger_release_v1', 'robot': 'F1-09', 'files': files}))
    profile = tmp_path/'robot.json'
    profile.write_text(json.dumps({'expected_machine_id': module.MACHINE_ID,
                                  'startup_script_sha256': module.digest(original.read_bytes()),
                                  'motion_enabled': False, 'other_nested': {'keep': [1, 2]}}))
    before = profile.read_bytes()
    kwargs = dict(profile=profile, startup_dir=startup, backup_dir=tmp_path/'backup', expected_profile_sha256=module.digest(before))
    return module, kwargs, before, original


def test_profile_apply_only_changes_startup_fields_and_exact_rollback(profile_install):
    module, kwargs, before, original = profile_install
    source_before = original.read_bytes(); result = module.apply(**kwargs)
    after = json.loads(kwargs['profile'].read_text()); old = json.loads(before)
    assert {k: v for k, v in after.items() if k not in module.FIELDS} == {k: v for k, v in old.items() if k not in module.FIELDS}
    assert result['processes_restarted'] is False
    assert (kwargs['backup_dir']/'profile.before.json').read_bytes() == before
    module.rollback(backup_dir=kwargs['backup_dir'])
    assert kwargs['profile'].read_bytes() == before
    assert original.read_bytes() == source_before
    assert module.rollback(backup_dir=kwargs['backup_dir'])['changed'] is False


@pytest.mark.parametrize('fault', ['profile', 'vendor', 'release'])
def test_install_refuses_changed_dependencies_without_overwriting_profile(profile_install, fault):
    module, kwargs, before, original = profile_install
    target = {'profile': kwargs['profile'], 'vendor': original, 'release': kwargs['startup_dir'].parent/'scripts/60_f1_camera_internal.sh'}[fault]
    target.write_bytes(target.read_bytes() + b' '); preserved = kwargs['profile'].read_bytes()
    with pytest.raises(ValueError): module.apply(**kwargs)
    assert kwargs['profile'].read_bytes() == preserved
    assert not kwargs['backup_dir'].exists()


def test_rollback_refuses_later_unrelated_edits(profile_install):
    module, kwargs, _, _ = profile_install; module.apply(**kwargs)
    changed = json.loads(kwargs['profile'].read_text()); changed['later_edit'] = True
    kwargs['profile'].write_text(json.dumps(changed)); preserved = kwargs['profile'].read_bytes()
    with pytest.raises(ValueError, match='edited after installation'): module.rollback(backup_dir=kwargs['backup_dir'])
    assert kwargs['profile'].read_bytes() == preserved


@pytest.fixture
def camera_shell(tmp_path):
    """Execute the real wrapper with fake machine files and a capture-only ros2."""
    (tmp_path/'scripts').mkdir(); (tmp_path/'configs').mkdir()
    (tmp_path/'configs/f1_camera_fastdds_internal.xml').write_bytes((ROOT/'configs/f1_camera_fastdds_internal.xml').read_bytes())
    (tmp_path/'machine-id').write_text('90429b8a96e04ef79dbc12175b538115\n')
    setup = tmp_path/'setup.bash'; setup.write_text(':\n')
    capture = tmp_path/'ros2.py'
    capture.write_text('import os,sys,json\nprint(json.dumps({"launched":sys.argv[1:],"rmw":os.environ["RMW_IMPLEMENTATION"],"dds":os.environ["FASTRTPS_DEFAULT_PROFILES_FILE"]}))\n')
    share = tmp_path/'share/launch'; share.mkdir(parents=True)
    for name in ['head_gemini330.launch.py', 'rs_launch_left_wrist_d405.py', 'rs_launch_right_wrist_d405.py']: (share/name).touch()
    package = tmp_path/'ament_index_python'; package.mkdir(); (package/'__init__.py').touch()
    (package/'packages.py').write_text(f'def get_package_share_directory(_name): return {str(share.parent)!r}\n')
    binary = tmp_path/'bin'; binary.mkdir()
    ip = binary/'ip'; ip.write_text('#!/bin/sh\necho \'[{"flags":["UP"],"addr_info":[{"local":"192.168.127.170"}]}]\'\n'); ip.chmod(0o755)
    text = (ROOT/'scripts/60_f1_camera_internal.sh').read_text().replace('/etc/machine-id', str(tmp_path/'machine-id'))
    for path in ['/opt/ros/jazzy/setup.bash', '/home/user/workspace/f1/install/setup.bash', '/home/user/workspace/camera/OrbbecSDK_ROS2/install/setup.bash', '/home/user/workspace/camera/ros2_ws/install/setup.bash']:
        text = text.replace(path, str(setup))
    text = text.replace('/opt/ros/jazzy/bin/ros2', str(capture))
    script = tmp_path/'scripts/wrapper.sh'; script.write_text(text)
    env = {k: v for k, v in os.environ.items() if not k.startswith(('ROS_', 'RMW_', 'FASTRTPS_'))}
    env.update(PYTHONPATH=str(tmp_path), PATH=str(binary)+':'+env['PATH'])
    return script, env


@pytest.mark.parametrize('side', ['head', 'left', 'right'])
def test_camera_launch_preserves_head_and_pins_wrist_streams(camera_shell, side):
    script, env = camera_shell
    result = subprocess.run(['bash', str(script), 'start', side], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    launch = json.loads(result.stdout.splitlines()[-1]); assert launch['rmw'] == 'rmw_fastrtps_cpp'
    expected = [] if side == 'head' else ['depth_module.color_profile:=640,480,30', 'depth_module.depth_profile:=640,480,30']
    assert launch['launched'][3:] == expected
    assert not any('motor' in token for token in launch['launched'])


def test_camera_check_is_readonly_and_duplicate_wrist_node_is_rejected(camera_shell):
    script, env = camera_shell
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', '__ns:=/camera', '__node:=left_wrist'])
    try:
        check = subprocess.run(['bash', str(script), 'check', 'left'], env=env, capture_output=True, text=True)
        assert check.returncode == 0
        assert '"launched"' not in check.stdout
        assert child.pid in json.loads(check.stdout.splitlines()[0])['existing_camera_pids']
        start = subprocess.run(['bash', str(script), 'start', 'left'], env=env, capture_output=True, text=True)
        assert start.returncode != 0
        assert 'already running' in start.stderr
        assert '"launched"' not in start.stdout
    finally:
        child.terminate(); child.wait(timeout=5)

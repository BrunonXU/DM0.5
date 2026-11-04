"""Offline boundary and release checks; no SSH or running robot is involved."""
import ast
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy import package_shared, release


def test_shared_package_contains_local_root_import_dependencies():
    selected = set(package_shared.selected_files(ROOT))
    missing = []
    for path in selected:
        if path.parent != ROOT or path.suffix != '.py':
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            for module in modules:
                dependency = ROOT / (module.split('.')[0] + '.py')
                if dependency.is_file() and dependency not in selected:
                    missing.append(f'{path.name} imports missing {dependency.name}')
    assert not missing, '\n'.join(sorted(set(missing)))


def write(root, name, text='fixture\n'):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'standalone'
    for name in ('README.md', 'AGENTS.md', 'server.py', 'project_paths.py',
                 'configs/robot.example.json', 'configs/installation.env.example',
                 'web/index.html', 'integrations/pico_upper/src/pico_upper/__init__.py'):
        write(root, name)
    write(root, 'run.sh', '#!/bin/sh\necho fixture\n').chmod(0o755)
    for name in ('deploy/package_shared.py', 'deploy/release.py'):
        write(root, name, (ROOT / name).read_text())
    return root


def test_share_zip_is_standalone_verified_and_repackable(source, tmp_path):
    for name in ('runtime/private.json', 'results/old.zip', 'connect/client_ssh_config',
                 'unlisted/private.txt', 'configs/robot.json', 'configs/installation.env', 'configs/site.json',
                 'configs/access.token', 'configs/.env.local', 'web/.ssh/id_rsa',
                 'scripts/__pycache__/secret.pyc'):
        write(source, name, 'NOT FOR SHARING')
    write(source.parent, 'F1/AGENTS.md', 'PARENT WORKSPACE')
    result = package_shared.create_package(tmp_path / 'output', root=source, stamp='first')
    archive = Path(result['archive'])
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        assert all(name.startswith('F1-infra/') for name in z.namelist())
        assert z.read('F1-infra/README.md') == (source / 'README.md').read_bytes()
        assert stat.S_IMODE(z.getinfo('F1-infra/run.sh').external_attr >> 16) == 0o755
        assert 'F1-infra/project_paths.py' in z.namelist()
        assert not any(b'NOT FOR SHARING' in z.read(name) or b'PARENT WORKSPACE' in z.read(name)
                       for name in z.namelist())
        manifest = json.loads(z.read('F1-infra/MANIFEST.json'))
        for entry in manifest['files']:
            data = z.read('F1-infra/' + entry['path'])
            assert hashlib.sha256(data).hexdigest() == entry['sha256']
            assert len(data) == entry['bytes']
        for line in z.read('F1-infra/SHA256SUMS').decode().splitlines():
            checksum, name = line.split('  ', 1)
            assert hashlib.sha256(z.read('F1-infra/' + name)).hexdigest() == checksum
        z.extractall(tmp_path / 'elsewhere')
    extracted = tmp_path / 'elsewhere/F1-infra'
    # Execute the delivered tool from a directory with no original parent F1.
    completed = subprocess.run([sys.executable, str(extracted / 'deploy/package_shared.py'),
                                '--output-dir', str(tmp_path / 'repacked')],
                               cwd=tmp_path, text=True, capture_output=True, check=True)
    repacked = Path(json.loads(completed.stdout)['archive'])
    with zipfile.ZipFile(archive) as first, zipfile.ZipFile(repacked) as second:
        assert set(first.namelist()) == set(second.namelist())
        for name in first.namelist():
            if name.endswith(('/MANIFEST.json', '/SHA256SUMS')):
                continue
            assert first.read(name) == second.read(name)


@pytest.mark.parametrize('kind', ['file', 'directory'])
def test_source_symlinks_refused(source, tmp_path, kind):
    outside = write(tmp_path, 'outside/private.txt', 'private')
    link = source / 'web/escaped'
    link.symlink_to(outside if kind == 'file' else outside.parent, target_is_directory=kind != 'file')
    with pytest.raises(ValueError, match='Symlink'):
        package_shared.create_package(tmp_path / 'output', root=source, stamp=kind)
    assert not list((tmp_path / 'output').glob('*.zip'))


def test_private_key_marker_refused(source, tmp_path):
    write(source, 'configs/innocent.txt', '-----BEGIN OPENSSH PRIVATE KEY-----\nprivate\n')
    with pytest.raises(ValueError, match='Private key marker'):
        package_shared.create_package(tmp_path / 'output', root=source, stamp='key')
    assert not list((tmp_path / 'output').glob('*.zip'))


def test_local_release_has_one_project_and_keeps_explicit_local_configuration(source):
    write(source, 'configs/robot.json', '{"local":true}\n')
    write(source, 'configs/installation.env', 'export F1_INFRA_HOME=/example\n')
    output, archive, checksum = release.prepare_release('unit-local', source)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == checksum
    manifest = json.loads((output / 'manifest.json').read_text())
    with tarfile.open(archive) as tar:
        assert all(member.name == 'manifest.json' or member.name.startswith('workspace/F1-infra/')
                   for member in tar.getmembers())
        assert tar.getmember('workspace/F1-infra/configs/robot.json').isfile()
        assert tar.getmember('workspace/F1-infra/configs/installation.env').isfile()
        assert stat.S_IMODE(tar.getmember('workspace/F1-infra/run.sh').mode) == 0o755
        for entry in manifest['files']:
            data = tar.extractfile('workspace/' + entry['path']).read()
            assert hashlib.sha256(data).hexdigest() == entry['sha256']


@pytest.mark.parametrize('flag', ['--prepare-only', '--pack'])
def test_prepare_mode_never_constructs_remote(source, monkeypatch, flag):
    monkeypatch.setattr(release, 'ROOT', source)
    def forbidden(*args, **kwargs):
        raise AssertionError('Remote must not be created by local preparation')
    monkeypatch.setattr(release, 'Remote', forbidden)
    release.main(['--release', 'local-only', flag])
    assert (source / 'results/deployments/local-only/payload.tar.gz').is_file()


def test_release_requires_explicit_host_before_packaging(source, monkeypatch):
    monkeypatch.setattr(release, 'ROOT', source)
    with pytest.raises(SystemExit) as error:
        release.main(['--release', 'no-host'])
    assert error.value.code == 2
    assert not (source / 'results').exists()


@pytest.mark.parametrize('project_path', ['workspace/F1/F1-4242', 'workspace/F1-infra'])
def test_previous_manifest_supports_old_and_new_layout(tmp_path, project_path):
    base = tmp_path / 'deployment'
    previous = base / 'releases/old'
    current = previous / project_path
    current.mkdir(parents=True)
    write(previous, 'manifest.json', '{"release_id":"old"}')
    namespace = {}
    exec(release.REMOTE_HELPERS, namespace)
    assert namespace['prior_manifest'](base, current) == {'release_id': 'old'}


@pytest.mark.parametrize('stage_only', [False, True])
def test_remote_unpack_and_activation_code_offline(source, tmp_path, stage_only):
    _, archive, checksum = release.prepare_release('verified', source)
    base = tmp_path / 'remote'
    incoming = base / 'incoming/verified.tar.gz'
    incoming.parent.mkdir(parents=True)
    shutil.copyfile(archive, incoming)
    code = release.activate_script(str(base), 'verified', str(incoming), checksum,
                                   {'current': None}, stage_only)
    completed = subprocess.run([sys.executable, '-c', code], text=True,
                               capture_output=True, check=True)
    result = json.loads(completed.stdout)
    assert result['staged_only'] == stage_only
    assert (base / 'current').exists() is not stage_only
    deployed = base / 'releases/verified/workspace/F1-infra'
    assert (deployed / 'server.py').read_bytes() == (source / 'server.py').read_bytes()
    assert stat.S_IMODE((deployed / 'run.sh').stat().st_mode) == 0o755


def test_malicious_release_tar_does_not_write_outside_release(tmp_path):
    base = tmp_path / 'remote'
    archive = base / 'incoming/malicious.tar.gz'
    archive.parent.mkdir(parents=True)
    with tarfile.open(archive, 'w:gz') as tar:
        info = tarfile.TarInfo('../../escaped')
        info.size = 1
        tar.addfile(info, io.BytesIO(b'x'))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    code = release.activate_script(str(base), 'bad', str(archive), checksum,
                                   {'current': None}, True)
    completed = subprocess.run([sys.executable, '-c', code], text=True, capture_output=True)
    assert completed.returncode != 0
    assert 'archive path escaped release' in completed.stderr
    assert not (base / 'releases/bad').exists()
    assert not (tmp_path / 'escaped').exists()


def test_existing_non_release_entry_point_is_protected(tmp_path):
    base = tmp_path / 'deployment'
    (base / 'current').mkdir(parents=True)
    completed = subprocess.run([sys.executable, '-c', release.inspect_script(str(base))],
                               text=True, capture_output=True)
    assert completed.returncode != 0
    assert 'current must be a release symlink' in completed.stderr


def test_web_sync_requires_explicit_host():
    completed = subprocess.run([sys.executable, str(ROOT / 'scripts/watch_web.py'), '--once'],
                               text=True, capture_output=True)
    assert completed.returncode == 2
    assert '--host' in completed.stderr


def test_model_source_is_bundled_integration():
    spec = importlib.util.spec_from_file_location('f1_prepare_model', ROOT / 'scripts/prepare_model.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.SOURCE == ROOT / 'integrations/pico_upper'


def test_activation_refuses_a_running_previous_release(tmp_path):
    base = tmp_path / 'deployment'
    current = base / 'releases/old/workspace/F1/F1-4242'
    current.mkdir(parents=True)
    (base / 'current').symlink_to(current)
    namespace = {}
    exec(release.REMOTE_HELPERS, namespace)
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], cwd=current)
    try:
        with pytest.raises(RuntimeError, match='task running; no activation permitted'):
            namespace['require_inactive'](base, current)
        assert (base / 'current').resolve() == current
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_web_sync_publishes_parseable_state_with_explicit_connection(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('f1_watch_web', ROOT / 'scripts/watch_web.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / 'source'
    write(source, 'web/index.html', 'fixture web')
    remote_root = tmp_path / 'remote/dev/web'
    ssh_config = write(tmp_path, 'external-ssh-config')
    monkeypatch.setattr(module, 'ROOT', source)
    monkeypatch.setattr(sys, 'argv', ['watch_web.py', '--once', '--host', 'example-host',
                                     '--ssh-config', str(ssh_config), '--remote', str(remote_root)])
    calls = []
    def local_remote(code, ssh):
        calls.append(code)
        assert ssh == ['ssh', '-F', str(ssh_config), 'example-host', 'python3 -']
        return subprocess.run([sys.executable, '-c', code], check=True,
                              text=True, capture_output=True).stdout
    monkeypatch.setattr(module, 'remote', local_remote)
    module.main()
    assert (remote_root / 'index.html').read_text() == 'fixture web'
    state = json.loads((remote_root.parent / 'sync.json').read_text())
    assert state['files']['index.html'] == hashlib.sha256(b'fixture web').hexdigest()
    calls.clear()
    module.main()
    assert len(calls) == 1  # The next invocation reads state without re-uploading assets.

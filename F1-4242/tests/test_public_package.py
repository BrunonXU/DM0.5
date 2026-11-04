"""Boundary and reproducibility checks for the outward-facing source package."""
import importlib.util
import json
from pathlib import Path
import stat
import zipfile

import pytest


SPEC = importlib.util.spec_from_file_location('package_public', Path(__file__).parents[1] / 'deploy/package_public.py')
packager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packager)


def write(root, name, value=b'public\n', mode=0o644):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    path.chmod(mode)
    return path


@pytest.fixture
def inputs(tmp_path):
    root = tmp_path / 'source'
    docs = tmp_path / 'public-docs'
    for name in packager.REQUIRED_FILES - {'README.md'}:
        write(root, name, b'{}\n' if name.endswith('.json') else b'# public\n', 0o755 if name.endswith('.sh') else 0o644)
    write(root, 'README.md', b'private internal README\n')
    write(docs, 'README.md', b'# F1-infra\n\nPublic deployment steps.\n')
    write(docs, 'docs/STRUCTURE.md', b'# Structure\n\nPublic code layout.\n')
    return root, docs


def build(tmp_path, inputs, **kwargs):
    source, docs = inputs
    return packager.create_package(source_dir=source, docs_dir=docs, output_dir=tmp_path / 'package', **kwargs)


def test_only_runtime_code_and_reviewed_docs_are_delivered(tmp_path, inputs):
    root, _ = inputs
    private = ['AGENTS.md', 'tests/test_robot.py', 'results/request.json', 'docs/STATUS.md',
               'docs/INTERNAL.md', 'configs/f1-09.json', 'configs/f1-09-joints.json',
               'configs/robot.json', 'configs/site.json', 'configs/installation.env',
               'scripts/check_browser.cjs', 'scripts/watch_web.py', 'deploy/release.py',
               'web/review/private.html', 'recording/LOCAL_CHANGES.patch',
               'integrations/pico_upper/tests/test_private.py',
               'integrations/pico_upper/docs/OPERATIONS.md', '.git/config']
    kept = ['robot/runtime_contract.py', 'recording/UPSTREAM.json',
            'integrations/pico_upper/UPSTREAM.json', 'integrations/pico_upper/RELEASE_FILES.json',
            'integrations/pico_upper/scripts/env.sh', 'web/models/f1_wheel/manifest.json',
            'web/vendor/three/LICENSE']
    for name in private:
        write(root, name, b'private internal record\n')
    for name in kept:
        write(root, name)
    result = build(tmp_path, inputs, extract_dir=tmp_path / 'preview')
    unpacked = tmp_path / 'preview/F1-infra'
    for name in private:
        assert not (unpacked / name).exists(), name
    for name in kept:
        assert (unpacked / name).is_file(), name
    assert (unpacked / 'README.md').read_text().startswith('# F1-infra')
    assert (unpacked / 'docs/STRUCTURE.md').is_file()
    assert stat.S_IMODE((unpacked / 'run.sh').stat().st_mode) == 0o755
    manifest = json.loads((unpacked / 'MANIFEST.json').read_text())
    assert set(manifest) == {'package', 'version', 'date', 'files'}
    assert all(set(f) == {'path', 'bytes', 'sha256', 'mode'} for f in manifest['files'])
    assert str(root) not in (unpacked / 'MANIFEST.json').read_text()
    assert result['privacy_passed']


def test_reproducible_zip_and_zip_input_preserve_bytes_and_modes(tmp_path, inputs):
    first = build(tmp_path, inputs)
    second = packager.create_package(source_zip=first['archive'], docs_dir=inputs[1], output_dir=tmp_path / 'second')
    assert first['sha256'] == second['sha256']
    assert Path(first['archive']).read_bytes() == Path(second['archive']).read_bytes()
    assert inputs[0].joinpath('README.md').read_text() == 'private internal README\n'


@pytest.mark.parametrize('payload', [
    b'/home/private_operator_a/projects/private', b'/home/private_operator_b/Bruno/F1',
    b'192.0.2.3', b'00000000000000000000000000000000',
    b'-----BEGIN RSA PRIVATE KEY-----', b'C:\\Users\\private_operator_a\\private',
])
def test_sensitive_source_fails_without_creating_archive(tmp_path, inputs, payload):
    write(inputs[0], 'server.py', b'# ' + payload)
    report = tmp_path / 'audit.json'
    with pytest.raises(ValueError, match='Privacy audit'):
        build(tmp_path, inputs, audit_report=report)
    findings = json.loads(report.read_text())['findings']
    assert findings and findings[0]['path'] == 'server.py'
    assert not (tmp_path / 'package').exists()
    assert inputs[0].joinpath('server.py').read_bytes() == b'# ' + payload


def test_audit_only_and_additional_identifier(tmp_path, inputs):
    write(inputs[0], 'server.py', b'# new private identifier\n')
    result = build(tmp_path, inputs, audit_only=True, deny_text=['new private identifier'])
    assert result['findings'] == 1 and not result['archive_created']
    assert not (tmp_path / 'package').exists()


@pytest.mark.parametrize('name, mode', [
    ('F1-infra/../../outside.py', stat.S_IFREG | 0o644),
    ('/F1-infra/server.py', stat.S_IFREG | 0o644),
    ('F1-infra/./server.py', stat.S_IFREG | 0o644),
    ('F1-infra\\server.py', stat.S_IFREG | 0o644),
    ('F1-infra/server.py', stat.S_IFLNK | 0o777),
    ('F1-infra/server.py', stat.S_IFIFO | 0o644),
])
def test_rejects_unsafe_zip_members(tmp_path, name, mode):
    source = tmp_path / 'unsafe.zip'
    with zipfile.ZipFile(source, 'w') as archive:
        info = zipfile.ZipInfo(name)
        info.external_attr = mode << 16
        archive.writestr(info, b'../outside')
    with pytest.raises(ValueError):
        packager.read_zip(source)
    assert not (tmp_path / 'outside.py').exists()


def test_rejects_duplicate_zip_members(tmp_path):
    source = tmp_path / 'duplicate.zip'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('F1-infra/server.py', b'first')
        archive.writestr('F1-infra/SERVER.py', b'second')
    with pytest.raises(ValueError, match='Duplicate'):
        packager.read_zip(source)


def test_rejects_source_symlink_and_leaves_existing_outputs(tmp_path, inputs):
    source, _ = inputs
    source.joinpath('server.py').unlink()
    source.joinpath('server.py').symlink_to(tmp_path / 'outside.py')
    with pytest.raises(ValueError, match='Nonregular'):
        build(tmp_path, inputs)
    source.joinpath('server.py').unlink()
    write(source, 'server.py', b'# safe\n')
    first = build(tmp_path, inputs)
    original = Path(first['archive']).read_bytes()
    with pytest.raises(FileExistsError):
        build(tmp_path, inputs)
    assert Path(first['archive']).read_bytes() == original


def test_output_cannot_write_inside_source_or_reuse_preview(tmp_path, inputs):
    with pytest.raises(ValueError, match='read-only'):
        packager.create_package(source_dir=inputs[0], docs_dir=inputs[1], output_dir=inputs[0] / 'new')
    preview = tmp_path / 'preview'
    preview.mkdir()
    write(preview, 'keep.txt', b'keep')
    with pytest.raises(FileExistsError):
        build(tmp_path, inputs, extract_dir=preview)
    assert preview.joinpath('keep.txt').read_bytes() == b'keep'
    assert not (tmp_path / 'package').exists()


def test_public_docs_cannot_override_code(tmp_path, inputs):
    write(inputs[1], 'server.py', b'bad')
    with pytest.raises(ValueError, match='only accepts Markdown'):
        build(tmp_path, inputs)


def test_private_audit_cannot_be_written_in_public_preview(tmp_path, inputs):
    preview = tmp_path / 'preview'
    with pytest.raises(ValueError, match='outside the public preview'):
        build(tmp_path, inputs, extract_dir=preview, audit_report=preview / 'audit.json')
    assert not preview.exists()


def test_vendor_attribution_and_generic_network_examples_are_allowed(tmp_path, inputs):
    write(inputs[0], 'web/models/model.urdf', b'<!-- Original author: Jane Doe, jane@example.org -->')
    write(inputs[1], 'docs/NETWORK.md', b'Example: http://192.0.2.15:4242; local /home/user/F1-infra')
    assert build(tmp_path, inputs)['privacy_passed']

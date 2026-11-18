import hashlib
import io
import json
import tarfile

import pytest

import f1_harness.controller_reuse as reuse


def encoded(value):
    return json.dumps(value).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def tar_write(path, entries):
    with tarfile.open(path, 'w:gz') as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name); info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def fixture(tmp_path, monkeypatch):
    source = dict(ik=dict(verified=True, verification_note='existing acceptance', safety={'max_dt_s': .05}),
        hardware_capabilities={'left_gripper_present': False, 'right_gripper_present': True},
        manual_compute='process_v1', fault_lifecycle='f1_fault_lifecycle_v1',
        policy={'endpoint': 'do-not-inherit'}, rlt_policy={'endpoint': 'do-not-inherit'},
        acceptance={'profile_sha256': 'profile'}, accepted_release_manifest_sha256='old')
    source_raw = encoded(source)
    archive = tmp_path / 'v47.tar.gz'
    tar_write(archive, {reuse.SOURCE_CONFIG: source_raw,
        'assembly-manifest.json': encoded({'files': {reuse.SOURCE_CONFIG: sha(source_raw)}})})
    activation = tmp_path / 'activated.json'
    activation.write_bytes(encoded({'control_version': {'version': 47}, 'verified_files': 190}))
    trusted = dict(archive=reuse.file_hash(archive), activation=reuse.file_hash(activation),
                   config=sha(source_raw), urdf='urdf', mapping='mapping')
    monkeypatch.setattr(reuse, 'TRUSTED_V47', trusted)
    infra = encoded({'files': {reuse.MODEL_FILES['urdf']: 'urdf', reuse.MODEL_FILES['mapping']: 'mapping'}})
    control = encoded({'files': {'configs/f1_infra_binding.json': sha(infra)}})
    hashes = {'control/configs/f1_infra_binding.json': sha(infra), 'control/F1_RELEASE_MANIFEST.json': sha(control)}
    manifest = dict(bundle_id=sha(json.dumps(hashes, sort_keys=True).encode()), files=hashes,
                    control_manifest_sha256=sha(control))
    bundle = tmp_path / 'bundle.tar.gz'
    tar_write(bundle, {'manifest.json': encoded(manifest),
        'control/configs/f1_infra_binding.json': infra, 'control/F1_RELEASE_MANIFEST.json': control})
    site = tmp_path / 'original'; site.mkdir()
    controller = dict(ik=dict(verified=False, verification_note='new candidate', safety={'max_dt_s': .05}),
                      infra_manifest='configs/f1_infra_binding.json', policy={'endpoint': ''})
    (site / 'controller.local.json').write_bytes(encoded(controller))
    (site / 'launch.local.json').write_bytes(encoded({'controller_config': '/home/user/Bruno/HARNESS/site/original/controller.local.json'}))
    (site / 'numerics.local.json').write_bytes(encoded({'spec': 'preserved'}))
    (site / 'owner.local.json').write_bytes(encoded({'integration_validated': False}))
    prep = tmp_path / 'preparation.json'
    prep.write_bytes(encoded(dict(controller_sha256=reuse.file_hash(site / 'controller.local.json'),
        profile_sha256='profile', model_input=dict(source_urdf_sha256='urdf', source_mapping_sha256='mapping'))))
    return site, dict(robot_site='/home/user/Bruno/HARNESS/site/development', bundle_archive=bundle,
        source_archive=archive, activation_receipt=activation, native_preparation=prep)


def test_reuses_controller_settings_only_in_new_site(tmp_path, monkeypatch):
    source, kwargs = fixture(tmp_path, monkeypatch)
    (source / 'credentials.local.json').write_text('{}')
    before = (source / 'controller.local.json').read_bytes()
    output = tmp_path / 'copy'
    result = reuse.prepare_controller(source, output, **kwargs)
    value = json.loads((output / 'controller.local.json').read_text())
    assert value['ik']['verified'] and value['ik']['safety'] == {'max_dt_s': .05}
    assert value['hardware_capabilities']['left_gripper_present'] is False
    assert value['manual_compute'] == 'process_v1' and value['fault_lifecycle'] == 'f1_fault_lifecycle_v1'
    assert value['policy']['endpoint'] == '' and 'rlt_policy' not in value and 'acceptance' not in value
    assert value['accepted_release_manifest_sha256'] == result['candidate']['control_manifest_sha256'] != 'old'
    assert (source / 'controller.local.json').read_bytes() == before
    assert (output / 'owner.local.json').read_bytes() == (source / 'owner.local.json').read_bytes()
    assert not (output / 'credentials.local.json').exists()
    assert not result['new_harness_shadow_performed'] and not result['camera_precision_certified']
    assert result['command_publishers'] == 0
    with pytest.raises(FileExistsError):
        reuse.prepare_controller(source, output, **kwargs)


def test_changed_ik_does_not_inherit_verification(tmp_path, monkeypatch):
    source, kwargs = fixture(tmp_path, monkeypatch)
    path = source / 'controller.local.json'; value = json.loads(path.read_text())
    value['ik']['safety']['max_dt_s'] = 1; path.write_bytes(encoded(value))
    with pytest.raises(ValueError, match='ik_configuration_differs'):
        reuse.prepare_controller(source, tmp_path / 'copy', **kwargs)
    assert not (tmp_path / 'copy').exists()


@pytest.mark.parametrize('field,value,reason', [
    ('profile_sha256', 'wrong', 'profile_differs'),
    ('controller_sha256', 'wrong', 'controller_identity_mismatch'),
    ('model_input', {'source_urdf_sha256': 'wrong', 'source_mapping_sha256': 'mapping'}, 'model_identity_mismatch')])
def test_native_identity_must_match_real_preparation(tmp_path, monkeypatch, field, value, reason):
    source, kwargs = fixture(tmp_path, monkeypatch)
    path = kwargs['native_preparation']; prep = json.loads(path.read_text())
    prep[field] = value; path.write_bytes(encoded(prep))
    with pytest.raises(ValueError, match=reason):
        reuse.prepare_controller(source, tmp_path / 'copy', **kwargs)


def test_changed_deployment_source_is_refused(tmp_path, monkeypatch):
    source, kwargs = fixture(tmp_path, monkeypatch)
    kwargs['activation_receipt'].write_bytes(b'{}')
    with pytest.raises(ValueError, match='source_evidence_changed'):
        reuse.prepare_controller(source, tmp_path / 'copy', **kwargs)


def test_modified_bundle_source_is_refused(tmp_path, monkeypatch):
    source, kwargs = fixture(tmp_path, monkeypatch)
    path = kwargs['bundle_archive']
    with tarfile.open(path) as tar:
        entries = {name: tar.extractfile(name).read() for name in tar.getnames()}
    entries['control/configs/f1_infra_binding.json'] = b'{}'
    tar_write(path, entries)
    with pytest.raises(ValueError, match='bundle_member_hash_mismatch'):
        reuse.prepare_controller(source, tmp_path / 'copy', **kwargs)


def test_source_remote_site_and_path_escape_are_refused(tmp_path, monkeypatch):
    source, kwargs = fixture(tmp_path, monkeypatch)
    kwargs['robot_site'] = '/home/user/Bruno/HARNESS/site/original'
    with pytest.raises(ValueError, match='must_differ'):
        reuse.prepare_controller(source, tmp_path / 'copy', **kwargs)
    kwargs['robot_site'] = '/home/user/Bruno/HARNESS/site/../../F1'
    with pytest.raises(ValueError, match='explicit_new_robot_site'):
        reuse.prepare_controller(source, tmp_path / 'copy', **kwargs)

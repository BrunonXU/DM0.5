"""Reuse the unchanged, deployed F1 IK configuration in a new Harness site copy."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import tarfile


SOURCE_CONFIG = 'configs/v1_dagger_stage_b_1250-rlt-hil.json'
TRUSTED_V47 = dict(
    archive='ac2465c2037d34d3d4898e1a4dc497858d9e2db258e3d6a1b5b4bfc26b738e60',
    activation='940b5f878bcf861754d55f332d7a8c2c1cd7b5a923e170651f33da3837f11ef2',
    config='6fb05f4a5705346d8540c67c559a6d7c083c0bcecd83411705ea0e7e601da110',
    urdf='bc21c6f36446aa2741d6ba20c2190836be967b1f3043dfa39eaae0983c95d5c0',
    mapping='21e1879e2b5e15dfe590a27b1cb3c256626364b161cb950e24c5dd08a86633d5',
)
MODEL_FILES = dict(urdf='web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf',
                   mapping='configs/model_mapping.json')


def file_hash(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(data)
    return value.hexdigest()


def _member(tar, name):
    member = tar.getmember(name)
    if not member.isfile() or member.size > 8 * 1024**2:
        raise ValueError('configuration_archive_member_invalid')
    return tar.extractfile(member).read()


def _bundle_identity(path):
    with tarfile.open(path) as tar:
        names = tar.getnames()
        if len(names) != len(set(names)):
            raise ValueError('duplicate_bundle_member')
        manifest = json.loads(_member(tar, 'manifest.json'))
        hashes = manifest['files']
        expected_id = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        if expected_id != manifest['bundle_id'] or set(names) != set(hashes) | {'manifest.json'}:
            raise ValueError('bundle_manifest_invalid')
        for member in tar.getmembers():
            if (not member.isfile() or PurePosixPath(member.name).is_absolute()
                    or '..' in PurePosixPath(member.name).parts):
                raise ValueError('unsafe_bundle_member')
            if member.name == 'manifest.json':
                continue
            value = hashlib.sha256()
            with tar.extractfile(member) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    value.update(block)
            if value.hexdigest() != hashes[member.name]:
                raise ValueError('bundle_member_hash_mismatch')
        control = json.loads(_member(tar, 'control/F1_RELEASE_MANIFEST.json'))
        control_sha = hashes['control/F1_RELEASE_MANIFEST.json']
        if control_sha != manifest['control_manifest_sha256']:
            raise ValueError('bundle_control_manifest_mismatch')
        for name, expected in control['files'].items():
            if hashes.get('control/' + name) != expected:
                raise ValueError('control_source_not_bound_by_bundle')
        infra = json.loads(_member(tar, 'control/configs/f1_infra_binding.json'))
    return manifest, infra


def prepare_controller(source_site, output_site, *, robot_site, bundle_archive,
                       source_archive, activation_receipt, native_preparation):
    """Create only a new local site. No deploy, motion, new calibration or shadow claim."""
    source_site, output_site = Path(source_site), Path(output_site)
    if output_site.exists():
        raise FileExistsError('development_site_must_be_new')
    if (any(p.is_symlink() for p in (source_site, *source_site.parents))
            or any(p.is_symlink() for p in (output_site, *output_site.parents))):
        raise ValueError('site_symlink_forbidden')
    target = PurePosixPath(robot_site)
    if (not target.is_relative_to('/home/user/Bruno/HARNESS/site')
            or '..' in target.parts or len(target.parts) < 7):
        raise ValueError('explicit_new_robot_site_required')
    if file_hash(source_archive) != TRUSTED_V47['archive'] or file_hash(activation_receipt) != TRUSTED_V47['activation']:
        raise ValueError('deployed_source_evidence_changed')
    deployed = json.loads(Path(activation_receipt).read_text())
    if (deployed.get('control_version', {}).get('version') != 47
            or deployed.get('verified_files', 0) < 1):
        raise ValueError('source_deployment_receipt_invalid')
    with tarfile.open(source_archive) as tar:
        raw = _member(tar, SOURCE_CONFIG)
        source = json.loads(raw)
        source_manifest = json.loads(_member(tar, 'assembly-manifest.json'))
    if (hashlib.sha256(raw).hexdigest() != TRUSTED_V47['config']
            or source_manifest.get('files', {}).get(SOURCE_CONFIG) != TRUSTED_V47['config']):
        raise ValueError('deployed_controller_config_changed')
    if source['ik'].get('verified') is not True:
        raise ValueError('source_ik_unverified')
    candidate = json.loads((source_site / 'controller.local.json').read_text())
    strip = lambda value: {key: item for key, item in value.items() if key not in ('verified', 'verification_note')}
    if strip(candidate['ik']) != strip(source['ik']):
        raise ValueError('candidate_ik_configuration_differs_from_deployed')
    preparation = json.loads(Path(native_preparation).read_text())
    if preparation.get('controller_sha256') != file_hash(source_site / 'controller.local.json'):
        raise ValueError('native_preparation_controller_identity_mismatch')
    if preparation.get('profile_sha256') != source.get('acceptance', {}).get('profile_sha256'):
        raise ValueError('native_profile_differs_from_deployed_acceptance')
    manifest, infra = _bundle_identity(bundle_archive)
    model = preparation.get('model_input', {})
    for name, relative in MODEL_FILES.items():
        if (model.get('source_' + name + '_sha256') != TRUSTED_V47[name]
                or infra.get('files', {}).get(relative) != TRUSTED_V47[name]):
            raise ValueError('native_or_bundle_model_identity_mismatch:' + name)
    if candidate.get('infra_manifest') != 'configs/f1_infra_binding.json':
        raise ValueError('candidate_infra_binding_path_changed')
    # The reused fields describe the existing controller. Neither the V47 policy
    # endpoints nor its accepted release identity are copied into Harness.
    for key in ('hardware_capabilities', 'manual_compute', 'fault_lifecycle'):
        if key not in source:
            raise ValueError('deployed_controller_field_missing:' + key)
        candidate[key] = source[key]
    candidate['ik']['verified'] = True
    candidate['ik']['verification_note'] = (
        'Unchanged IK configuration and URDF/mapping/profile reused from deployed F1 V47; '
        'see controller-reuse.json. This is not Harness task, camera accuracy or new shadow acceptance.')
    candidate['accepted_release_manifest_sha256'] = manifest['control_manifest_sha256']
    configs = {}
    for name in ('controller', 'launch', 'numerics', 'owner', 'task', 'plan', 'verify', 'gateway', 'masks'):
        path = source_site / (name + '.local.json')
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError('site_configuration_not_regular')
        configs[path.name] = path.read_bytes()
    if not all(name in configs for name in ('controller.local.json', 'launch.local.json', 'numerics.local.json')):
        raise ValueError('native_prepared_site_required')
    launch = json.loads(configs['launch.local.json'])
    if launch.get('controller_config') == str(target / 'controller.local.json'):
        raise ValueError('new_robot_site_must_differ_from_source')
    launch['controller_config'] = str(target / 'controller.local.json')
    configs['controller.local.json'] = (json.dumps(candidate, indent=2) + '\n').encode()
    configs['launch.local.json'] = (json.dumps(launch, indent=2) + '\n').encode()
    receipt = dict(schema='f1-harness-controller-reuse-v1', source_control_version=47,
        source=dict(archive_path=str(Path(source_archive).resolve()), archive_sha256=TRUSTED_V47['archive'],
                    config_member=SOURCE_CONFIG, config_sha256=TRUSTED_V47['config'],
                    activation_receipt_path=str(Path(activation_receipt).resolve()),
                    activation_receipt_sha256=TRUSTED_V47['activation'],
                    previous_acceptance=source['acceptance']),
        candidate=dict(bundle_id=manifest['bundle_id'], control_manifest_sha256=manifest['control_manifest_sha256'],
            controller_sha256=hashlib.sha256(configs['controller.local.json']).hexdigest(),
            controller_path=str(target / 'controller.local.json')),
        native_preparation=dict(path=str(Path(native_preparation).resolve()), sha256=file_hash(native_preparation),
            profile_sha256=preparation['profile_sha256'], urdf_sha256=TRUSTED_V47['urdf'],
            mapping_sha256=TRUSTED_V47['mapping']),
        inherited_fields=['ik.verified', 'hardware_capabilities', 'manual_compute', 'fault_lifecycle'],
        ik_configuration_unchanged=True, new_harness_shadow_performed=False,
        physical_task_accepted=False, camera_precision_certified=False,
        production_activated=False, motion_authorized=False, command_publishers=0)
    output_site.mkdir(parents=True)
    for name, data in configs.items():
        (output_site / name).write_bytes(data)
    # Preserve other source evidence only by reference; private or arbitrary
    # files do not enter the copied site directory.
    (output_site / 'controller-reuse.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt

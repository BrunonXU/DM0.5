import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from f1_harness.contracts import PLACEMENT_SKILLS as SKILLS, Task, digest
from f1_harness.development import FULL_PARAMETER_PATHS, REVIEWED_FILES, validate_development
from f1_harness.frames import FrameChain
from f1_harness.readiness import inspect_site


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def site(tmp_path, monkeypatch):
    root = tmp_path / 'site'
    root.mkdir()
    bundle = tmp_path / 'bundle'
    source = bundle / 'src/f1_harness'
    source.mkdir(parents=True)
    (source / 'sample.py').write_text('SYNTHETIC_TEST_FIXTURE = True\n')
    control = bundle / 'control/F1_RELEASE_MANIFEST.json'
    control.parent.mkdir()
    control.write_text('{}')
    files = {str(p.relative_to(bundle)): sha(p) for p in (source / 'sample.py', control)}
    manifest = dict(files=files, bundle_id=hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest())
    (bundle / 'manifest.json').write_text(json.dumps(manifest))
    monkeypatch.setattr('f1_harness.development._runtime_root', lambda: bundle)
    urdf, mapping = tmp_path / 'model.urdf', tmp_path / 'mapping.json'
    urdf.write_text('test model'); mapping.write_text('{}')
    model = dict(urdf_path=str(urdf), urdf_sha256=sha(urdf),
                 mapping_path=str(mapping), mapping_sha256=sha(mapping))
    rgb_info = dict(width=640, height=480, frame_id='right_wrist_color_optical_frame',
                    distortion_model='plumb_bob', k=[390.,0.,320.,0.,390.,240.,0.,0.,1.],
                    d=[-.05,.05,0.,0.,-.01], r=np.eye(3).ravel().tolist(),
                    p=[390.,0.,320.,0.,0.,390.,240.,0.,0.,0.,1.,0.])
    depth_info = dict(rgb_info, frame_id='right_wrist_depth_optical_frame', d=[0.] * 5)
    camera_transform = np.eye(4); camera_transform[:3,3] = [.02,-.15,.09]
    tip_transform = np.eye(4); tip_transform[:3,3] = [.002,-.236,.008]
    right = dict(rgb_CameraInfo=rgb_info, depth_CameraInfo=depth_info,
                 T_Link7_camera=camera_transform.tolist(), T_rgb_depth=np.eye(4).tolist(),
                 depth_scale_candidate_m=.001)
    head = dict(rgb_CameraInfo=dict(rgb_info, frame_id='head_color_optical_frame', width=1280, height=720),
                depth_CameraInfo=dict(depth_info, frame_id='head_depth_optical_frame', width=848, height=480),
                T_world_camera=np.eye(4).tolist(), T_rgb_depth=np.eye(4).tolist(), depth_scale_candidate_m=.001)
    coarse = dict(schema='f1-coarse-calibration-handoff-v1', accepted_by_user=True,
                  delivery_complete=True, status='coarse/development',
                  not_a_runtime_calibration_bundle=True, formal_precision_certified=False,
                  calibration_data_id='coarse-test', executor_identity={k:v for k,v in model.items() if k.endswith('sha256')},
                  boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(), cameras={'right':right,'head':head},
                  body_head_reference_deg={**{'head_j'+str(i):0. for i in range(1,4)},
                                           **{'body_j'+str(i):0. for i in range(1,6)}},
                  tool_reference=dict(status='coarse/development', accepted_by_user=True,
                      tool='Test empty tool', camera_serial='test-serial', fixed_gripper_closure_feedback=18,
                      T_Link7_tip_reference=tip_transform.tolist()))
    calibration_path = root / 'coarse.json'
    calibration_path.write_text(json.dumps(coarse))
    reuse_path = root / 'ik-reuse.json'
    reuse_path.write_text(json.dumps(dict(test_fixture=True)))
    task = Task(task_id='approach-test', object_id='block', hole_id='unused-hole',
                calibration_id='coarse-test', goal='approach_grasp',
                allowed_skills=('approach_grasp', 'reobserve'), local_corrections=0, regrasp_limit=0)
    chain = dict(admission_profile='development_coarse', development_owner_config=str(root / 'owner.local.json'),
                 calibration_id='coarse-test', native_frame='world', ik_world_frame='world',
                 native_eef_frame='arm_R_Link7', ik_eef_frame='arm_R_Link7',
                 T_ik_world_native=np.eye(4).tolist(), T_ik_eef_native_eef=np.eye(4).tolist(),
                 independent_physical_accuracy_verified=False,
                 urdf_sha256=model['urdf_sha256'], mapping_sha256=model['mapping_sha256'])
    configs = dict(
        owner=dict(schema='f1-harness-owner-config-v1', admission_profile='development_coarse',
                   development_admission='development.local.json', calibration_bundle=None,
                   integration_validated=False, owner_socket='/owner', planner_socket='/plan',
                   verifier_socket='/verify', stop_delta_deg=.01, stop_window_ms=200,
                   stop_gripper_delta=.1, feedback_age_ms=200, reference_tolerance_deg=.5),
        task=task.model_dump(),
        plan=dict(task=task.model_dump(), socket='/plan', numerical_socket='/numerics',
                  fixture=dict(frame_chain=chain, native_frame='world', planning_margin_m=.01,
                               approach_clearance_m=.04, attached_radius_m=.1,
                               insertion_depth_m=None, effective_clearance_m=None, object_grasp_eef=None,
                               eef_tip_reference=tip_transform.tolist(), reference_gripper_closure=18,
                               maximum_approach_translation_m=.02)),
        verify=dict(task=task.model_dump(), socket='/verify'),
        numerics=dict(socket='/numerics', spec={'fixture':True}), masks={'fixture':True},
        gateway=dict(owner_socket='/owner', observation=dict(source_clock_verified=False, cameras=[dict(name='right')]),
                     scene=dict(owner_socket='/owner', numerical_socket='/numerics', verifier_socket='/verify',
                                frame_chain=chain, geometry=dict(calibration_id='coarse-test', native_frame='world',
                                    object_id='block', hole_id='unused-hole', goal='approach_grasp',
                                    planning_margin_m=.01, hole_clearance_m=None, fixture_occlusion=None,
                                    camera='right', base_camera=None, eef_camera=camera_transform.tolist(),
                                    k_rgb=np.asarray(rgb_info['k']).reshape(3,3).tolist(), d_rgb=rgb_info['d'],
                                    k_depth=np.asarray(depth_info['k']).reshape(3,3).tolist(), d_depth=depth_info['d'],
                                    rgb_depth=right['T_rgb_depth'], depth_scale_m=.001,
                                    rgb_info_sha256=hashlib.sha256(json.dumps(rgb_info,sort_keys=True).encode()).hexdigest(),
                                    depth_info_sha256=hashlib.sha256(json.dumps(depth_info,sort_keys=True).encode()).hexdigest()))),
        controller=dict(ik=dict(verified=True), accepted_release_manifest_sha256=sha(control)),
        launch={'fixture':True})
    record = dict(schema='f1-harness-development-admission-v1',
                  user_acceptance=dict(coarse=True, skip_shadow=True, statement='Test fixture only', evidence_id='test'),
                  scope=dict(goal='approach_grasp', allowed_skills=['approach_grasp','reobserve'],
                             empty_gripper=True, no_gripper_motion=True),
                  calibration=dict(path='coarse.json', sha256=sha(calibration_path), data_id='coarse-test'),
                  controller_reuse_receipt=dict(path='ik-reuse.json', sha256=sha(reuse_path)),
                  model=model, code=dict(bundle_manifest_path=str(bundle / 'manifest.json'),
                      bundle_id=manifest['bundle_id'], control_manifest_sha256=sha(control)))

    def seal():
        for name, value in configs.items():
            (root / (name + '.local.json')).write_text(json.dumps(value))
        record['reviewed_files'] = {name: sha(root / name) for name in REVIEWED_FILES}
        record['task_hash'] = digest(configs['task'])
        (root / 'development.local.json').write_text(json.dumps(record))

    seal()
    return dict(root=root, configs=configs, record=record, chain=chain, seal=seal, bundle=bundle, coarse=coarse)


def test_development_receipt_does_not_claim_precision_or_authorize(site):
    receipt = validate_development(site['root'] / 'owner.local.json')
    assert receipt['goal'] == 'approach_grasp' and receipt['skip_shadow_accepted']
    assert not any(receipt[key] for key in ('motion_authorized','precision_certified','integration_validated','shadow_completed'))
    report = inspect_site(site['root'])
    assert report['static_configuration_complete']
    assert report['admission_profile'] == 'development_coarse'
    assert not report['motion_authorized']


@pytest.mark.parametrize('acceptance', ['coarse', 'skip_shadow'])
def test_each_explicit_user_acceptance_is_required(site, acceptance):
    site['record']['user_acceptance'][acceptance] = False
    site['seal']()
    with pytest.raises(ValueError, match='explicit_coarse_and_skip_shadow'):
        validate_development(site['root'] / 'owner.local.json')


@pytest.mark.parametrize('name', REVIEWED_FILES)
def test_any_reviewed_configuration_change_is_rejected(site, name):
    path = site['root'] / name
    path.write_text(path.read_text() + '\n')
    with pytest.raises(ValueError, match='development_reviewed_file_changed'):
        validate_development(site['root'] / 'owner.local.json')


def test_modified_coarse_packet_is_rejected(site):
    path = site['root'] / 'coarse.json'
    path.write_text(path.read_text() + '\n')
    with pytest.raises(ValueError, match='development_file_identity_changed:coarse'):
        validate_development(site['root'] / 'owner.local.json')


def test_extra_grasp_skill_cannot_expand_approved_approach(site):
    site['configs']['task']['allowed_skills'] = ('approach_grasp', 'reobserve', 'grasp')
    site['seal']()
    with pytest.raises(ValueError, match='exact noncontact skill scope'):
        validate_development(site['root'] / 'owner.local.json')


def test_verified_ik_is_still_required(site):
    site['configs']['controller']['ik']['verified'] = False
    site['seal']()
    with pytest.raises(ValueError, match='verified_existing_controller'):
        validate_development(site['root'] / 'owner.local.json')


def test_running_source_change_invalidates_admission(site):
    (site['bundle'] / 'src/f1_harness/sample.py').write_text('CHANGED = True\n')
    with pytest.raises(ValueError, match='running_harness_source_changed'):
        validate_development(site['root'] / 'owner.local.json')


def test_model_change_invalidates_development(site):
    Path(site['record']['model']['urdf_path']).write_text('different model')
    with pytest.raises(ValueError, match='model_identity_changed:urdf'):
        validate_development(site['root'] / 'owner.local.json')


def test_same_executor_frame_contract_is_identity_not_precision(site):
    frame = FrameChain.load(site['chain'])
    assert frame.native_frame == 'world'
    np.testing.assert_array_equal(frame.world_native, np.eye(4))
    site['chain']['T_ik_eef_native_eef'][0][3] = .01
    site['seal']()
    with pytest.raises(ValueError, match='frame_identity_only'):
        FrameChain.load(site['chain'])


def test_sdk_frame_cannot_be_used_with_executor_coarse_packet(site):
    site['chain']['native_frame'] = 'native_right'
    site['seal']()
    with pytest.raises(ValueError, match='identical_executor_frames_required'):
        FrameChain.load(site['chain'])


def test_development_does_not_exempt_missing_feedback_threshold(site):
    site['configs']['owner']['stop_gripper_delta'] = None
    site['seal']()
    report = inspect_site(site['root'])
    owner = next(row for row in report['components'] if row['component']=='owner')
    assert not owner['ready']
    assert 'unfilled:.stop_gripper_delta' in owner['reasons']


def test_unknown_planning_margin_cannot_be_invented_by_admission(site):
    site['configs']['gateway']['scene']['geometry']['planning_margin_m'] = None
    site['seal']()
    with pytest.raises(ValueError, match='positive_planning_geometry_required'):
        validate_development(site['root'] / 'owner.local.json')


def test_owner_can_prepare_development_without_granting_start(site, monkeypatch, tmp_path):
    from f1_harness.control import build_bridge
    # Exercise composition and the real engine; socket serving is unnecessary here.
    monkeypatch.setattr('f1_harness.owner_bridge.OwnerBridge.serve', lambda *_: None)
    bridge = build_bridge(site['root'] / 'owner.local.json', tmp_path / 'run', powered=True)
    try:
        engine = bridge.engine
        assert engine.hardware_ready and not engine.authorized
        assert engine.required_task_hash == site['record']['task_hash']
        assert engine.required_calibration_id == 'coarse-test'
        session = engine.open_session()
        result = engine.prepare_task(session['session_id'], site['configs']['task'])
        assert not result['authorized']
        assert engine.development_admission['precision_certified'] is False
    finally:
        bridge.close()
        bridge.journal.close()


@pytest.mark.parametrize('key,value,reason', [
    ('camera', 'head', 'dynamic_right_camera'),
    ('base_camera', np.eye(4).tolist(), 'dynamic_right_camera'),
    ('eef_camera', np.eye(4).tolist(), 'right_handeye'),
    ('k_rgb', np.eye(3).tolist(), 'rgb_intrinsics'),
    ('d_depth', [.01] * 5, 'depth_distortion'),
    ('rgb_info_sha256', 'bad-hash', 'camera_info_identity'),
    ('depth_info_sha256', 'bad-hash', 'camera_info_identity'),
    ('depth_scale_m', 1., 'depth_scale'),
])
def test_right_camera_inputs_cannot_drift_from_coarse_packet(site, key, value, reason):
    site['configs']['gateway']['scene']['geometry'][key] = value
    site['seal']()
    with pytest.raises(ValueError, match=reason):
        validate_development(site['root'] / 'owner.local.json')


@pytest.mark.parametrize('key,value,reason', [
    ('reference_gripper_closure', 20, 'fixed_empty_tool_reference'),
    ('eef_tip_reference', np.eye(4).tolist(), 'empty_tip_reference'),
    ('maximum_approach_translation_m', .03001, 'approach_step_exceeds_scope'),
    ('maximum_approach_translation_m', 0, 'approach_step_exceeds_scope'),
])
def test_empty_tool_reference_and_step_limit_remain_bound(site, key, value, reason):
    site['configs']['plan']['fixture'][key] = value
    site['seal']()
    with pytest.raises(ValueError, match=reason):
        validate_development(site['root'] / 'owner.local.json')


def test_native_reboot_requires_explicit_recheck(site):
    site['coarse']['boot_id'] = 'another-boot'
    path = site['root'] / 'coarse.json'
    path.write_text(json.dumps(site['coarse']))
    site['record']['calibration']['sha256'] = sha(path)
    site['seal']()
    with pytest.raises(ValueError, match='robot_boot_changed'):
        validate_development(site['root'] / 'owner.local.json')


def test_development_scope_cannot_allow_gripper_movement(site):
    site['record']['scope']['no_gripper_motion'] = False
    site['seal']()
    with pytest.raises(ValueError, match='empty_approach_scope'):
        validate_development(site['root'] / 'owner.local.json')


@pytest.fixture
def full_site(site):
    configs, record = site['configs'], site['record']
    task = Task(task_id='full-test', object_id='block', hole_id='unused-hole',
                calibration_id='coarse-test', goal='placed', admission_profile='development_coarse')
    configs['task'] = task.model_dump()
    configs['plan']['task'] = task.model_dump()
    configs['verify']['task'] = task.model_dump()
    configs['plan']['fixture'].update(
        object_dimensions_m=[.03,.03,.06], hole_inner_dimensions_m=[.035,.035],
        insertion_depth_m=.015, effective_clearance_m=.0025, transfer_clearance_m=.10,
        grasp_opening=75, release_opening=18, object_grasp_eef=np.eye(4).tolist(),
        eef_tip_reference=None, reference_gripper_closure=None, maximum_approach_translation_m=None)
    configs['gateway']['scene']['geometry'].update(
        goal='placed', admission_profile='development_coarse', precision_certified=False,
        base_hole=np.eye(4).tolist(), face_object=np.eye(4).tolist(),
        hole_clearance_m=.0025, insertion_depth_m=.015, uncertainty_m=None,
        xy_tolerance_m=.004, yaw_tolerance_rad=.1, tilt_tolerance_rad=.1,
        hole_reprojection_px=3., gripper_open_range=[0,20], tracking_tolerance_m=.005)
    head = site['coarse']['cameras']['head']
    configs['gateway']['scene']['geometry']['hole_camera'] = dict(
        camera='head', base_camera=head['T_world_camera'], rgb_depth=head['T_rgb_depth'],
        roi=[307,28,1229,720], depth_scale_m=.001,
        **{'k_'+stream:head[stream+'_CameraInfo']['k'] for stream in ('rgb','depth')},
        **{'d_'+stream:head[stream+'_CameraInfo']['d'] for stream in ('rgb','depth')},
        **{stream+'_info_sha256':hashlib.sha256(json.dumps(head[stream+'_CameraInfo'],sort_keys=True).encode()).hexdigest()
           for stream in ('rgb','depth')})
    configs['gateway']['scene'].update(full22_reference=[0.] * 22, reference_tolerance_deg=.5)
    record['scope'] = dict(goal='placed', allowed_skills=list(SKILLS), local_corrections=2, regrasp_limit=1)
    record['parameter_sources'] = {path:dict(basis='estimated', evidence_ids=['test-only-image'],
                                                   note='Synthetic test configuration, not physical evidence.')
                                   for path in FULL_PARAMETER_PATHS}
    record['parameter_sources']['plan.fixture.insertion_depth_m']['basis'] = 'configured'
    record['metadata'] = dict(physical_hole_depth_m=None)
    site['seal']()
    return site


def test_full_coarse_development_requires_no_precision_certificate_or_step_cap(full_site):
    receipt = validate_development(full_site['root'] / 'owner.local.json')
    assert receipt['goal']=='placed' and receipt['allowed_skills']==list(SKILLS)
    assert receipt['local_corrections']==2 and receipt['regrasp_limit']==1
    assert not receipt['no_gripper_motion'] and not receipt['empty_gripper']
    assert not receipt['precision_certified'] and not receipt['shadow_completed']
    assert receipt['scope_kind']=='full_single_block_development'
    report = inspect_site(full_site['root'])
    assert report['static_configuration_complete'] and not report['motion_authorized']
    assert full_site['configs']['gateway']['scene']['geometry']['uncertainty_m'] is None
    assert 'full single-block development' in report['next_gate']
    # The same executor frame is usable without inventing a separate certificate.
    assert FrameChain.load(full_site['chain']).native_frame=='world'


@pytest.mark.parametrize('path', FULL_PARAMETER_PATHS)
def test_full_task_parameters_require_recorded_sources(full_site, path):
    del full_site['record']['parameter_sources'][path]
    full_site['seal']()
    with pytest.raises(ValueError, match='parameter_source_required'):
        validate_development(full_site['root'] / 'owner.local.json')


def test_full_gripper_configuration_cannot_be_missing(full_site):
    full_site['configs']['plan']['fixture']['grasp_opening'] = None
    full_site['seal']()
    with pytest.raises(ValueError, match='full_parameter_missing'):
        validate_development(full_site['root'] / 'owner.local.json')
    assert not inspect_site(full_site['root'])['static_configuration_complete']


def test_full_profile_must_be_explicit_in_task(full_site):
    for name in ('task','plan','verify'):
        task=full_site['configs'][name] if name=='task' else full_site['configs'][name]['task']
        task['admission_profile']='precision'
    full_site['seal']()
    with pytest.raises(ValueError, match='full_task_profile_or_budget_mismatch'):
        validate_development(full_site['root'] / 'owner.local.json')


def test_full_scene_cannot_claim_precision_profile(full_site):
    full_site['configs']['gateway']['scene']['geometry']['admission_profile']='precision'
    full_site['seal']()
    with pytest.raises(ValueError, match='full_scene_profile_required'):
        validate_development(full_site['root'] / 'owner.local.json')


def test_full_development_still_rejects_wrong_model_identity(full_site):
    Path(full_site['record']['model']['mapping_path']).write_text('{"changed":true}')
    with pytest.raises(ValueError, match='model_identity_changed:mapping'):
        validate_development(full_site['root'] / 'owner.local.json')


def test_full_head_camera_info_cannot_drift(full_site):
    full_site['configs']['gateway']['scene']['geometry']['hole_camera']['rgb_info_sha256']='wrong'
    full_site['seal']()
    with pytest.raises(ValueError, match='head_camera_info_identity_mismatch'):
        validate_development(full_site['root'] / 'owner.local.json')


def test_full_head_reference_must_match_coarse_posture(full_site):
    full_site['configs']['gateway']['scene']['full22_reference'][15]=2.
    full_site['seal']()
    with pytest.raises(ValueError, match='head_body_reference_mismatch'):
        validate_development(full_site['root'] / 'owner.local.json')


def test_full_development_cannot_claim_precision_certified(full_site):
    full_site['configs']['gateway']['scene']['geometry']['precision_certified']=True
    full_site['seal']()
    with pytest.raises(ValueError, match='full_scene_profile_required'):
        validate_development(full_site['root'] / 'owner.local.json')


def test_unknown_uncertainty_is_not_exempted_without_valid_development(full_site):
    full_site['record']['user_acceptance']['coarse']=False
    full_site['seal']()
    report=inspect_site(full_site['root'])
    assert report['admission_profile']=='development_coarse'
    assert not report['static_configuration_complete']
    assert report['components'][0]['component']=='development_admission'
    assert 'explicit_coarse_and_skip_shadow' in report['components'][0]['reasons'][0]


@pytest.mark.parametrize('fault',['none','different','too_large','nan'])
def test_coarse_depth_band_binds_verifier_to_existing_motion_goal(full_site,fault):
    configs=full_site['configs'];fixture=configs['plan']['fixture'];geometry=configs['gateway']['scene']['geometry']
    fixture['object_planning_margin_m']=.0005
    fixture['revalidation']=dict(xy_tolerance_m=.002,yaw_tolerance_rad=.1,tilt_tolerance_rad=.1)
    fixture['insertion_depth_tolerance_m']=.002;geometry['insertion_depth_tolerance_m']=.002
    if fault=='different':geometry['insertion_depth_tolerance_m']=.001
    if fault=='too_large':fixture['insertion_depth_tolerance_m']=geometry['insertion_depth_tolerance_m']=.003
    if fault=='nan':fixture['insertion_depth_tolerance_m']=float('nan')
    full_site['seal']()
    if fault=='none':assert validate_development(full_site['root']/'owner.local.json')['goal']=='placed'
    else:
        with pytest.raises(ValueError,match='depth_tolerance'):
            validate_development(full_site['root']/'owner.local.json')

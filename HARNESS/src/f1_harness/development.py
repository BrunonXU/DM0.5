"""Explicit, immutable admission for coarse development tasks.

This contract is not a precision calibration certificate or an authorization event.
The existing owner still requires a fresh local START and all controller protections.
"""
from pathlib import Path
import hashlib
import json
import math
import time

from .contracts import SKILLS, PLACEMENT_SKILLS, Task, digest


PROFILE = 'development_coarse'
ALLOWED_SKILLS = ('approach_grasp', 'reobserve')
FULL_PARAMETER_PATHS = (
    'plan.fixture.object_dimensions_m', 'plan.fixture.hole_inner_dimensions_m',
    'plan.fixture.insertion_depth_m', 'plan.fixture.grasp_opening', 'plan.fixture.release_opening',
    'plan.fixture.object_grasp_eef', 'gateway.scene.geometry.base_hole',
    'gateway.scene.geometry.face_object',
)
REVIEWED_FILES = ('owner.local.json', 'task.local.json', 'plan.local.json', 'verify.local.json',
                  'gateway.local.json', 'numerics.local.json', 'masks.local.json',
                  'controller.local.json', 'launch.local.json')


def _load(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('development_regular_file_required:' + path.name)
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('development_object_required:' + path.name)
    return value, hashlib.sha256(raw).hexdigest()


def _path(base, value):
    if not isinstance(value, str) or not value:
        raise ValueError('development_file_path_required')
    candidate = Path(value)
    return candidate if candidate.is_absolute() else base / candidate


def _bound_file(base, record, label):
    if not isinstance(record, dict):
        raise ValueError('development_bound_file_required:' + label)
    path = _path(base, record.get('path'))
    value, sha = _load(path)
    if sha != record.get('sha256'):
        raise ValueError('development_file_identity_changed:' + label)
    return value


def _runtime_root():
    return Path(__file__).resolve().parents[2]


def _same_numeric(actual, expected, label, *, size=None):
    import numpy as np
    try:
        left, right = (np.asarray(value, dtype=float).reshape(-1) for value in (actual, expected))
    except (ValueError, TypeError):
        raise ValueError('development_numeric_binding_invalid:' + label) from None
    if (not left.size or left.shape != right.shape or size is not None and left.size != size
            or not np.isfinite(left).all() or not np.isfinite(right).all()
            or not np.allclose(left, right, rtol=0, atol=1e-12)):
        raise ValueError('development_numeric_binding_mismatch:' + label)


def _check_right_camera_and_tool(calibration, geometry, fixture, *, goal):
    if geometry.get('camera') != 'right' or geometry.get('base_camera') is not None:
        raise ValueError('development_requires_dynamic_right_camera')
    right = calibration.get('cameras', {}).get('right', {})
    _same_numeric(geometry.get('eef_camera'), right.get('T_Link7_camera'), 'right_handeye', size=16)
    _same_numeric(geometry.get('rgb_depth'), right.get('T_rgb_depth'), 'right_rgb_depth', size=16)
    for stream in ('rgb', 'depth'):
        info = right.get(stream + '_CameraInfo')
        if not isinstance(info, dict):
            raise ValueError('development_coarse_camera_info_missing:' + stream)
        _same_numeric(geometry.get('k_' + stream), info.get('k'), stream + '_intrinsics', size=9)
        _same_numeric(geometry.get('d_' + stream), info.get('d'), stream + '_distortion')
        expected_hash = hashlib.sha256(json.dumps(info, sort_keys=True).encode()).hexdigest()
        if geometry.get(stream + '_info_sha256') != expected_hash:
            raise ValueError('development_camera_info_identity_mismatch:' + stream)
    _same_numeric(geometry.get('depth_scale_m'), right.get('depth_scale_candidate_m'), 'depth_scale', size=1)
    tool = calibration.get('tool_reference', {})
    if (tool.get('accepted_by_user') is not True
            or tool.get('status') != 'coarse/development'
            or not isinstance(tool.get('tool'), str) or not tool['tool']
            or not isinstance(tool.get('camera_serial'), str) or not tool['camera_serial']):
        raise ValueError('development_fixed_empty_tool_reference_required')
    if goal == 'placed':
        # Full development uses the registered grasp transform and then the
        # measured in-hand offset. The empty-tip reference is not a loaded TCP.
        return dict(camera='right', camera_serial=tool['camera_serial'], tool=tool['tool'],
                    reference_kind='grasp_configuration_then_measured_in_hand')
    if tool.get('fixed_gripper_closure_feedback') != 18 or fixture.get('reference_gripper_closure') != 18:
        raise ValueError('development_fixed_empty_tool_reference_required')
    _same_numeric(fixture.get('eef_tip_reference'), tool.get('T_Link7_tip_reference'), 'empty_tip_reference', size=16)
    step = fixture.get('maximum_approach_translation_m')
    if isinstance(step, bool) or not isinstance(step, (int, float)) or not math.isfinite(step) or not 0 < step <= .03:
        raise ValueError('development_approach_step_exceeds_scope')
    return dict(camera='right', camera_serial=tool['camera_serial'], tool=tool['tool'],
                reference_gripper_closure=18, maximum_approach_translation_m=step)


def _check_full_head_camera(calibration, scene):
    import numpy as np
    geometry = scene['geometry']
    camera = geometry.get('hole_camera')
    if not isinstance(camera, dict) or camera.get('camera') != 'head':
        raise ValueError('development_registered_head_hole_camera_required')
    head = calibration.get('cameras', {}).get('head', {})
    from .head_kinematics import checked_head_model
    checked_head_model(scene,calibration)
    _same_numeric(camera.get('base_camera'), head.get('T_world_camera'), 'head_extrinsics', size=16)
    _same_numeric(camera.get('rgb_depth'), head.get('T_rgb_depth'), 'head_rgb_depth', size=16)
    for stream in ('rgb', 'depth'):
        info = head.get(stream + '_CameraInfo')
        if not isinstance(info, dict):
            raise ValueError('development_head_camera_info_missing:' + stream)
        _same_numeric(camera.get('k_' + stream), info.get('k'), 'head_' + stream + '_intrinsics', size=9)
        _same_numeric(camera.get('d_' + stream), info.get('d'), 'head_' + stream + '_distortion')
        expected = hashlib.sha256(json.dumps(info, sort_keys=True).encode()).hexdigest()
        if camera.get(stream + '_info_sha256') != expected:
            raise ValueError('development_head_camera_info_identity_mismatch:' + stream)
    _same_numeric(camera.get('depth_scale_m'), head.get('depth_scale_candidate_m'), 'head_depth_scale', size=1)
    roi = camera.get('roi')
    if (not isinstance(roi, list) or len(roi)!=4 or any(type(v) is not int for v in roi)
            or not 0 <= roi[0] < roi[2] <= head['rgb_CameraInfo']['width']
            or not 0 <= roi[1] < roi[3] <= head['rgb_CameraInfo']['height']):
        raise ValueError('development_head_roi_invalid')
    reference = np.asarray(scene.get('full22_reference'), dtype=float)
    tolerance = scene.get('reference_tolerance_deg')
    names = tuple('head_j'+str(i) for i in range(1,4)) + tuple('body_j'+str(i) for i in range(1,6))
    body = calibration.get('body_head_reference_deg', {})
    if (reference.shape != (22,) or not np.isfinite(reference).all()
            or any(name not in body for name in names)
            or isinstance(tolerance, bool) or not isinstance(tolerance, (int,float))
            or not math.isfinite(tolerance) or tolerance <= 0):
        raise ValueError('development_head_body_reference_required')
    if np.max(np.abs(reference[14:] - np.asarray([body[name] for name in names]))) > tolerance:
        raise ValueError('development_head_body_reference_mismatch')
    tracking = geometry.get('tracking_tolerance_m')
    if (isinstance(tracking, bool) or not isinstance(tracking, (int,float))
            or not math.isfinite(tracking) or tracking <= 0):
        raise ValueError('development_relative_tracking_tolerance_required')


def _check_full_parameters(record, configs):
    import numpy as np
    from .geometry import transform
    fixture = configs['plan.local.json']['fixture']
    geometry = configs['gateway.local.json']['scene']['geometry']
    if geometry.get('admission_profile') != PROFILE or geometry.get('precision_certified') is not False:
        raise ValueError('development_full_scene_profile_required')
    sources = record.get('parameter_sources', {})
    for path in FULL_PARAMETER_PATHS:
        component, *keys = path.split('.')
        value = configs[component + '.local.json']
        for key in keys:
            value = value.get(key) if isinstance(value, dict) else None
        source = sources.get(path) if isinstance(sources, dict) else None
        if value is None:
            raise ValueError('development_full_parameter_missing:' + path)
        if (not isinstance(source, dict) or source.get('basis') not in ('measured', 'estimated', 'configured')
                or not isinstance(source.get('evidence_ids'), list) or not source['evidence_ids']
                or any(not isinstance(item, str) or not item.strip() for item in source['evidence_ids'])
                or not isinstance(source.get('note'), str) or not source['note'].strip()):
            raise ValueError('development_parameter_source_required:' + path)
    for key, size in (('object_dimensions_m', 3), ('hole_inner_dimensions_m', 2)):
        value = np.asarray(fixture[key], dtype=float)
        if value.shape != (size,) or not np.isfinite(value).all() or np.any(value <= 0):
            raise ValueError('development_positive_dimensions_required:' + key)
    for key in ('insertion_depth_m', 'transfer_clearance_m'):
        value = fixture.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError('development_positive_full_geometry_required:' + key)
    if fixture['insertion_depth_m'] != geometry.get('insertion_depth_m'):
        raise ValueError('development_insertion_depth_binding_mismatch')
    depth_band=fixture.get('insertion_depth_tolerance_m',0.)
    if (type(depth_band) not in (int,float) or not math.isfinite(depth_band) or depth_band<0
            or depth_band!=geometry.get('insertion_depth_tolerance_m',0.)
            or depth_band>=fixture['insertion_depth_m']):
        raise ValueError('development_insertion_depth_tolerance_binding_mismatch')
    if depth_band:
        from .revalidation import tolerances
        if depth_band>tolerances(fixture,'insert')['goal_position_m']:
            raise ValueError('development_insertion_depth_tolerance_exceeds_goal')

    for key in ('grasp_opening', 'release_opening'):
        value = fixture[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError('development_gripper_configuration_invalid:' + key)
    for value in (fixture['object_grasp_eef'], geometry['base_hole'], geometry['face_object']):
        transform(value)
    return {path: sources[path] for path in FULL_PARAMETER_PATHS}


def _check_code(code, base):
    if not isinstance(code, dict):
        raise ValueError('development_code_identity_required')
    manifest_path = _path(base, code.get('bundle_manifest_path'))
    manifest, _ = _load(manifest_path)
    files = manifest.get('files')
    if (not isinstance(files, dict) or not files
            or hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest() != code.get('bundle_id')
            or manifest.get('bundle_id') != code.get('bundle_id')
            or files.get('control/F1_RELEASE_MANIFEST.json') != code.get('control_manifest_sha256')):
        raise ValueError('development_bundle_identity_changed')
    root = manifest_path.resolve().parent
    runtime = _runtime_root()
    native = (runtime / 'manifest.json').is_file()
    if native and manifest_path.resolve() != (runtime / 'manifest.json').resolve():
        raise ValueError('development_requires_current_native_bundle')
    # An offline inspection may read a copied manifest, but it must still match
    # every currently loaded Harness source file. Native execution checks the
    # complete pinned bundle as well (including the controller and shared core).
    source = runtime / 'src/f1_harness'
    actual = {str(p.relative_to(runtime)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in source.rglob('*.py') if '__pycache__' not in p.parts}
    expected = {k: v for k, v in files.items() if k.startswith('src/f1_harness/') and k.endswith('.py')}
    if not actual or actual != expected:
        raise ValueError('development_running_harness_source_changed')
    for relative, expected in files.items():
        if native and relative.startswith(('src/', 'native/', 'control/', 'upstream/', 'configs/')):
            path = root / relative
            if (path.is_symlink() or not path.resolve().is_relative_to(root)
                    or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
                raise ValueError('development_bundle_source_changed:' + relative)


def validate_development(config_path):
    """Return a bound development receipt, never a formal calibration certificate."""
    config_path = Path(config_path).resolve()
    owner, _ = _load(config_path)
    if owner.get('admission_profile') != PROFILE:
        raise ValueError('explicit_development_profile_required')
    if owner.get('calibration_bundle') is not None or owner.get('integration_validated') is not False:
        raise ValueError('development_must_preserve_unvalidated_precision_status')
    path = _path(config_path.parent, owner.get('development_admission'))
    record, admission_sha = _load(path)
    base = path.parent
    if record.get('schema') != 'f1-harness-development-admission-v1':
        raise ValueError('development_admission_schema_required')
    acceptance = record.get('user_acceptance', {})
    if (acceptance.get('coarse') is not True or acceptance.get('skip_shadow') is not True
            or not isinstance(acceptance.get('statement'), str) or not acceptance['statement'].strip()
            or not isinstance(acceptance.get('evidence_id'), str) or not acceptance['evidence_id'].strip()):
        raise ValueError('explicit_coarse_and_skip_shadow_acceptance_required')
    scope = record.get('scope', {})
    goal = scope.get('goal')
    if goal == 'placed':
        allowed_skills = tuple(scope.get('allowed_skills', ()))
        if (allowed_skills not in (PLACEMENT_SKILLS, PLACEMENT_SKILLS+('withdraw_reset',),
                                  PLACEMENT_SKILLS+('withdraw_reset','move_tcp'),
                                  PLACEMENT_SKILLS+('withdraw_reset','move_tcp','recover_grasp'), SKILLS)
                or type(scope.get('local_corrections')) is not int
                or not 0 <= scope['local_corrections'] <= 2 or scope.get('regrasp_limit') != 1):
            raise ValueError('development_full_task_scope_required')
    elif goal == 'approach_grasp':
        allowed_skills = ALLOWED_SKILLS
        if (tuple(scope.get('allowed_skills', ())) != allowed_skills
                or scope.get('empty_gripper') is not True or scope.get('no_gripper_motion') is not True):
            raise ValueError('development_empty_approach_scope_required')
    else:
        raise ValueError('development_goal_required')
    calibration = _bound_file(base, record.get('calibration'), 'coarse_calibration')
    data_id = calibration.get('calibration_data_id')
    if (calibration.get('schema') != 'f1-coarse-calibration-handoff-v1'
            or calibration.get('accepted_by_user') is not True
            or calibration.get('delivery_complete') is not True
            or calibration.get('status') != 'coarse/development'
            or calibration.get('not_a_runtime_calibration_bundle') is not True
            or calibration.get('formal_precision_certified') is not False
            or not isinstance(data_id, str) or not data_id
            or record['calibration'].get('data_id') != data_id):
        raise ValueError('accepted_coarse_calibration_identity_required')
    reviewed = record.get('reviewed_files', {})
    if not isinstance(reviewed, dict) or set(reviewed) != set(REVIEWED_FILES):
        raise ValueError('development_complete_site_review_required')
    site = config_path.parent
    configs = {}
    for name in REVIEWED_FILES:
        value, sha = _load(site / name)
        if sha != reviewed[name]:
            raise ValueError('development_reviewed_file_changed:' + name)
        configs[name] = value
    task = Task.model_validate(configs['task.local.json'])
    if (getattr(task, 'goal', None) != goal or task.allowed_skills != allowed_skills
            or task.calibration_id != data_id or digest(task) != record.get('task_hash')):
        raise ValueError('development_task_scope_or_identity_changed')
    if goal == 'placed' and (getattr(task, 'admission_profile', None) != PROFILE
                            or task.local_corrections != scope['local_corrections'] or task.regrasp_limit != 1):
        raise ValueError('development_full_task_profile_or_budget_mismatch')
    for name in ('plan.local.json', 'verify.local.json'):
        if Task.model_validate(configs[name]['task']) != task:
            raise ValueError('development_service_task_mismatch')
    geometry = configs['gateway.local.json']['scene']['geometry']
    if geometry.get('calibration_id') != data_id or geometry.get('goal') != scope['goal']:
        raise ValueError('development_scene_calibration_mismatch')
    fixture = configs['plan.local.json']['fixture']
    for value in (geometry.get('planning_margin_m'), fixture.get('planning_margin_m'),
                  fixture.get('approach_clearance_m'), fixture.get('attached_radius_m')):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError('development_positive_planning_geometry_required')
    if geometry['planning_margin_m'] != fixture['planning_margin_m']:
        raise ValueError('development_planning_margin_mismatch')
    if fixture['approach_clearance_m'] <= fixture['planning_margin_m']:
        raise ValueError('development_approach_clearance_below_margin')
    parameter_sources = _check_full_parameters(record, configs) if goal == 'placed' else {}
    if goal == 'placed':
        _check_full_head_camera(calibration, configs['gateway.local.json']['scene'])
    right_reference = _check_right_camera_and_tool(calibration, geometry, fixture, goal=goal)
    boot_id = calibration.get('boot_id')
    if not isinstance(boot_id, str) or not boot_id:
        raise ValueError('development_capture_boot_identity_required')
    native_boot_checked = (_runtime_root() / 'manifest.json').is_file()
    boot_recheck = None
    if native_boot_checked:
        current_boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if boot_id != current_boot_id and not record.get('boot_recheck'):
            raise ValueError('development_robot_boot_changed_recheck_required')
        if record.get('boot_recheck'):
            from .boot_recheck import validate_boot_recheck
            recheck = _bound_file(base, record['boot_recheck'], 'boot_recheck')
            boot_recheck = validate_boot_recheck(recheck, base=base, calibration=calibration,
                calibration_sha256=record['calibration']['sha256'],
                scene=configs['gateway.local.json']['scene'], task_hash=digest(task),
                current_boot_id=current_boot_id, now_ns=time.monotonic_ns())
    model = record.get('model', {})
    executor = calibration.get('executor_identity', {})
    for label in ('urdf', 'mapping'):
        model_path = _path(base, model.get(label + '_path'))
        expected = model.get(label + '_sha256')
        if (not isinstance(expected, str) or expected != executor.get(label + '_sha256')
                or model_path.is_symlink() or not model_path.is_file()
                or hashlib.sha256(model_path.read_bytes()).hexdigest() != expected):
            raise ValueError('development_model_identity_changed:' + label)
    _check_code(record.get('code'), base)
    controller = configs['controller.local.json']
    if (controller.get('ik', {}).get('verified') is not True
            or controller.get('accepted_release_manifest_sha256') != record['code']['control_manifest_sha256']):
        raise ValueError('development_verified_existing_controller_required')
    _bound_file(base, record.get('controller_reuse_receipt'), 'controller_reuse_receipt')
    return dict(admission_profile=PROFILE, admission_sha256=admission_sha,
                calibration_id=data_id, task_hash=digest(task), goal=scope['goal'],
                allowed_skills=list(allowed_skills), empty_gripper=goal=='approach_grasp',
                no_gripper_motion=goal=='approach_grasp',
                scope_kind='full_single_block_development' if goal=='placed' else 'empty_approach_development',
                local_corrections=task.local_corrections, regrasp_limit=task.regrasp_limit,
                precision_certified=False, integration_validated=False, shadow_completed=False,
                skip_shadow_accepted=True, motion_authorized=False,
                model=model, bundle_id=record['code']['bundle_id'], boot_id=boot_id,
                native_boot_checked=native_boot_checked, right_reference=right_reference,
                boot_recheck=boot_recheck,
                parameter_sources=parameter_sources)


def load_development_frame(record):
    """Same-model identities only; no claim of independent physical frame accuracy."""
    import numpy as np
    if not isinstance(record, dict) or record.get('admission_profile') != PROFILE:
        raise ValueError('explicit_development_frame_required')
    owner = record.get('development_owner_config')
    if not isinstance(owner, str) or not Path(owner).is_absolute():
        raise ValueError('development_frame_owner_binding_required')
    receipt = validate_development(owner)
    if (record.get('calibration_id') != receipt['calibration_id']
            or record.get('native_frame') != 'world' or record.get('ik_world_frame') != 'world'
            or record.get('native_eef_frame') != 'arm_R_Link7' or record.get('ik_eef_frame') != 'arm_R_Link7'
            or record.get('independent_physical_accuracy_verified') is not False):
        raise ValueError('development_identical_executor_frames_required')
    for key in ('T_ik_world_native', 'T_ik_eef_native_eef'):
        value = np.asarray(record.get(key), float)
        if value.size != 16 or not np.array_equal(value.reshape(4, 4), np.eye(4)):
            raise ValueError('development_frame_identity_only')
    if (record.get('urdf_sha256') != receipt['model']['urdf_sha256']
            or record.get('mapping_sha256') != receipt['model']['mapping_sha256']):
        raise ValueError('development_frame_model_binding_mismatch')
    return receipt

import json
from pathlib import Path
import pytest
from f1_harness.contracts import digest
from f1_harness.readiness import inspect_site
from f1_harness.reporting import summarize_run


def test_missing_site_never_reports_motion_ready(tmp_path):
    report=inspect_site(tmp_path)
    assert not report['static_configuration_complete'] and not report['motion_authorized']
    assert len(report['components'])==6


def test_report_does_not_count_planner_claim_or_hide_takeover(tmp_path):
    (tmp_path/'result.json').write_text(json.dumps({'planner_declaration':{'success':True}}))
    (tmp_path/'trace.jsonl').write_text(json.dumps({'kind':'authority_revoked','reason':'manual_takeover'})+'\n')
    report=summarize_run(tmp_path)
    assert report['assisted'] and not report['physical_success_claimed']


def _binding_fixture(tmp_path):
    # Configuration-only fixture. These declarations are not physical evidence;
    # inspect_site must keep live/physical admission and authority false.
    bundle={name:dict(accepted=True,synthetic=False,evidence_ids=['fixture-only']) for name in (
        'head_intrinsics','right_intrinsics','right_handeye','tcp','head_extrinsics',
        'rgbd_registration','native_frame_chain','scene_geometry','tracking_validation')}
    bundle.update(effective_clearance_m=.005,combined_error_bound_m=.001)
    (tmp_path/'bundle.json').write_text(json.dumps(bundle))
    identity=[1.,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1]
    chain=dict(accepted=True,synthetic=False,evidence_ids=['fixture-only'],holdout_hash='fixture-only',
        native_frame='native',ik_world_frame='world',native_eef_frame='eef',ik_eef_frame='link',
        T_ik_world_native=identity,T_ik_eef_native_eef=identity)
    task=dict(task_id='binding-test',object_id='block',hole_id='hole',shape='square',calibration_id=digest(bundle))
    configs=dict(
        owner=dict(owner_socket='/runtime/owner.sock',planner_socket='/runtime/plan.sock',
                   verifier_socket='/runtime/verify.sock',integration_validated=True,
                   stop_delta_deg=.01,stop_window_ms=100,stop_gripper_delta=.01,calibration_bundle='bundle.json'),
        task=task,plan=dict(socket='/runtime/plan.sock',numerical_socket='/runtime/numerics.sock',task=task,
                           fixture=dict(frame_chain=chain,native_frame='native')),
        verify=dict(socket='/runtime/verify.sock',task=task),
        numerics=dict(socket='/runtime/numerics.sock',spec={'test_fixture':True}),
        gateway=dict(owner_socket='/runtime/owner.sock',observation=dict(source_clock_verified=True,cameras=['fixture']),
            scene=dict(owner_socket='/runtime/owner.sock',numerical_socket='/runtime/numerics.sock',
                verifier_socket='/runtime/verify.sock',frame_chain=chain,
                geometry=dict(native_frame='native',object_id='block',hole_id='hole',calibration_id=digest(bundle)))))
    for name,config in configs.items():(tmp_path/(name+'.local.json')).write_text(json.dumps(config))
    return configs


def test_static_binding_pass_does_not_authorize_motion(tmp_path):
    _binding_fixture(tmp_path)
    report=inspect_site(tmp_path)
    assert report['static_configuration_complete']
    assert not report['motion_authorized'] and not report['physical_admission_checked']
    assert not report['live_services_checked']


@pytest.mark.parametrize('component,path,check',[
    ('gateway','scene.owner_socket','owner_socket_binding'),
    ('owner','planner_socket','plan_socket_binding'),
    ('owner','verifier_socket','verify_socket_binding'),
    ('numerics','socket','numerical_socket_binding'),
    ('gateway','scene.geometry.calibration_id','scene_calibration_binding'),
    ('gateway','scene.geometry.object_id','scene_object_binding'),
    ('gateway','scene.geometry.hole_id','scene_hole_binding'),
    ('plan','fixture.native_frame','native_frame_binding'),
])
def test_cross_service_mismatch_is_rejected_before_start(tmp_path,component,path,check):
    configs=_binding_fixture(tmp_path)
    target=configs[component]
    keys=path.split('.')
    for key in keys[:-1]:target=target[key]
    target[keys[-1]]='different-binding'
    (tmp_path/(component+'.local.json')).write_text(json.dumps(configs[component]))
    report=inspect_site(tmp_path)
    assert not report['static_configuration_complete']
    assert next(row for row in report['components'] if row['component']==check)['ready'] is False


def test_same_task_id_cannot_hide_different_calibration_bundle(tmp_path):
    configs=_binding_fixture(tmp_path)
    configs['task']['calibration_id']='different-calibration'
    configs['gateway']['scene']['geometry']['calibration_id']='different-calibration'
    for name in ('task','plan','verify','gateway'):
        (tmp_path/(name+'.local.json')).write_text(json.dumps(configs[name]))
    report=inspect_site(tmp_path)
    row=next(row for row in report['components'] if row['component']=='bundle_calibration_binding')
    assert not row['ready'] and not report['static_configuration_complete']


def test_example_verifier_socket_is_wired_to_actual_service():
    root=Path(__file__).resolve().parents[1]/'configs'
    configs={name:json.loads((root/(name+'.example.json')).read_text()) for name in ('owner','verify','gateway')}
    assert configs['owner']['verifier_socket']==configs['verify']['socket']==configs['gateway']['scene']['verifier_socket']

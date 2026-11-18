"""Synthetic fixtures for validating real-evidence contracts, never physical claims."""
import copy
import json
from pathlib import Path
import time

import pytest

from test_development_admission import full_site, site, sha  # noqa: F401
from f1_harness.boot_recheck import build_boot_receipt, validate_boot_recheck
from f1_harness.development import validate_development


@pytest.fixture
def capture(full_site):  # noqa: F811
    fixture = full_site
    cal = fixture['coarse']
    cal['boot_id'] = 'prior-test-boot'
    cal['camera_device_info'] = [
        dict(service='/camera/head/get_device_info', result=dict(success=True, info=dict(serial_number='head-test'))),
        dict(service='/camera/right_wrist/device_info', result=dict(serial_number='right-test')),
    ]
    cal_path = fixture['root'] / 'coarse.json'
    cal_path.write_text(json.dumps(cal))
    fixture['record']['calibration']['sha256'] = sha(cal_path)
    fixture['seal']()
    now = time.monotonic_ns()
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    raw = dict(schema='f1-harness-boot-capture-v1', current_boot_id=boot,
        observed_monotonic_ns=now, capture_boot_id=cal['boot_id'], calibration_sha256=sha(cal_path),
        calibration_data_id=cal['calibration_data_id'], model=cal['executor_identity'],
        camera_info={cam:{stream:cal['cameras'][cam][stream+'_CameraInfo'] for stream in ('rgb','depth')}
                     for cam in ('head','right')},
        camera_serials=dict(head='head-test',right='right-test'), body_head_deg=dict(cal['body_head_reference_deg']),
        synthetic=False, command_publishers=0, motion_authorized=False, precision_certified=False,
        complete_window=True, finished_boot_id=boot, errors=[], camera_info_changes=[],
        window_start_received_ns=now-600_000_000,
        camera_info_received_ns={cam:dict(rgb=now-10,depth=now-10) for cam in ('head','right')},
        joint_samples=[dict(received_ns=now-age, source_ns=now-age,
                            body_head_deg=dict(cal['body_head_reference_deg']))
                       for age in (500_000_000,100_000_000)])
    params = dict(calibration=cal, calibration_sha256=sha(cal_path),
        scene=fixture['configs']['gateway']['scene'], task_hash=fixture['record']['task_hash'],
        current_boot_id=boot, now_ns=now+1)
    path = fixture['root'] / 'boot-evidence.json'
    path.write_text(json.dumps(raw))
    return fixture, raw, path, params


def bind(capture):
    fixture, raw, path, params = capture
    path.write_text(json.dumps(raw))
    receipt = build_boot_receipt(raw,path,**params)
    target = fixture['root'] / 'boot-recheck.json'
    target.write_text(json.dumps(receipt))
    fixture['record']['boot_recheck'] = dict(path=target.name,sha256=sha(target))
    fixture['seal']()
    return receipt


def test_new_boot_receipt_preserves_original_calibration_identity(capture):
    fixture, _, _, _ = capture
    path = fixture['root'] / 'coarse.json'
    before = path.read_bytes()
    bind(capture)
    result = validate_development(fixture['root']/'owner.local.json')
    assert path.read_bytes() == before
    assert result['boot_id'] == 'prior-test-boot'
    assert result['boot_recheck']['current_boot_id'] != result['boot_id']
    assert not result['precision_certified'] and not result['motion_authorized']


@pytest.mark.parametrize('field,value,reason', [
    ('current_boot_id','different-boot','boot_or_calibration'),
    ('capture_boot_id','not-original','boot_or_calibration'),
    ('calibration_sha256','0'*64,'boot_or_calibration'),
    ('synthetic',True,'read_only_real'),
    ('motion_authorized',True,'read_only_real'),
    ('precision_certified',True,'read_only_real'),
    ('command_publishers',1,'read_only_real'),
    ('complete_window',False,'complete_capture'),
    ('finished_boot_id','another-reboot','complete_capture'),
])
def test_wrong_boot_or_claims_do_not_admit(capture,field,value,reason):
    capture[1][field]=value
    with pytest.raises(ValueError,match=reason):bind(capture)


def test_body_head_change_reports_actual_joint(capture):
    capture[1]['body_head_deg']['head_j2']=1.
    with pytest.raises(ValueError,match='body_head_reference_changed:head_j2'):
        bind(capture)


@pytest.mark.parametrize('kind,reason',[('serial','camera_device'),('intrinsics','camera_info_changed'),
                                      ('model','model_identity')])
def test_changed_hardware_or_model_rejected(capture,kind,reason):
    _,raw,_,_=capture
    if kind=='serial':raw['camera_serials']['head']='another-device'
    elif kind=='intrinsics':
        raw['camera_info']=copy.deepcopy(raw['camera_info'])
        raw['camera_info']['right']['rgb']['k'][0]+=1
    else:raw['model']=dict(raw['model'],urdf_sha256='0'*64)
    with pytest.raises(ValueError,match=reason):bind(capture)


def test_receipt_cannot_cover_another_task(capture):
    fixture,_,_,params=capture
    receipt=bind(capture)
    with pytest.raises(ValueError,match='task_binding'):
        validate_boot_recheck(receipt,base=fixture['root'],**dict(params,task_hash='0'*64))


def test_changed_raw_bytes_rejected(capture):
    fixture,_,path,_=capture
    bind(capture)
    path.write_text(path.read_text()+' ')
    with pytest.raises(ValueError,match='raw_evidence_binding'):
        validate_development(fixture['root']/'owner.local.json')


def test_stale_camera_or_incomplete_joint_window_rejected(capture):
    raw=capture[1]
    raw['camera_info_received_ns']['head']['rgb']=raw['observed_monotonic_ns']-550_000_000
    with pytest.raises(ValueError,match='camera_info_not_fresh'):bind(capture)
    raw['camera_info_received_ns']['head']['rgb']=raw['observed_monotonic_ns']-1
    raw['joint_samples'][0]['received_ns']=raw['joint_samples'][-1]['received_ns']-10
    with pytest.raises(ValueError,match='joint_window_incomplete'):bind(capture)


def test_transient_wrong_posture_cannot_be_hidden_by_last_sample(capture):
    capture[1]['joint_samples'][0]['body_head_deg']['body_j3']=3.
    with pytest.raises(ValueError,match='joint_window_reference_changed:body_j3'):
        bind(capture)

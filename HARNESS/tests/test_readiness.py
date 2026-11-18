import json

import pytest

from f1_harness.readiness import inspect_site


@pytest.fixture
def gateway(tmp_path):
    # Static test declarations only; this does not certify physical calibration.
    identity = [1., 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    chain = dict(accepted=True, synthetic=False, evidence_ids=['test-only'],
                 holdout_hash='test-only', native_frame='world', ik_world_frame='world',
                 native_eef_frame='link7', ik_eef_frame='link7',
                 T_ik_world_native=identity, T_ik_eef_native_eef=identity)
    config = dict(observation=dict(source_clock_verified=True, cameras=['test'], joint_state=None),
                  scene=dict(frame_chain=chain, geometry=dict(calibration_id='test',
                                                              fixture_occlusion=None)))

    def inspect():
        path = tmp_path / 'gateway.local.json'
        path.write_text(json.dumps(config))
        before = path.read_bytes()
        report = inspect_site(tmp_path)
        assert path.read_bytes() == before
        assert not report['motion_authorized']
        return next(row for row in report['components'] if row['component'] == 'gateway')

    return config, inspect


def _enabled():
    return dict(validated=True, evidence_ids=['test-only'], calibration_id='test', fixture_id='bin',
                max_occlusion_ns=500_000_000, pixel_tolerance=2., depth_tolerance_m=.003,
                anchors={name: dict(roi_center_px=point, depth_m=.4)
                         for name, point in [('a', [0., 0.]), ('b', [10., 0.]), ('c', [0., 10.])]})


def test_disabled_optional_occlusion_does_not_block_gateway(gateway):
    _, inspect = gateway
    assert inspect()['ready']


def test_complete_enabled_occlusion_is_still_inspected(gateway):
    config, inspect = gateway
    config['scene']['geometry']['fixture_occlusion'] = _enabled()
    assert inspect()['ready']


@pytest.mark.parametrize('field', ['evidence_ids', 'calibration_id', 'anchors', 'pixel_tolerance'])
def test_enabled_occlusion_missing_field_is_rejected(gateway, field):
    config, inspect = gateway
    occlusion = _enabled()
    del occlusion[field]
    config['scene']['geometry']['fixture_occlusion'] = occlusion
    row = inspect()
    assert not row['ready']
    assert 'unfilled:.scene.geometry.fixture_occlusion.' + field in row['reasons']


@pytest.mark.parametrize('missing', [True, False])
def test_enabled_occlusion_anchor_depth_cannot_be_omitted_or_null(gateway, missing):
    config, inspect = gateway
    occlusion = _enabled()
    if missing:
        del occlusion['anchors']['a']['depth_m']
    else:
        occlusion['anchors']['a']['depth_m'] = None
    config['scene']['geometry']['fixture_occlusion'] = occlusion
    row = inspect()
    assert not row['ready']
    assert 'unfilled:.scene.geometry.fixture_occlusion.anchors.a.depth_m' in row['reasons']


@pytest.mark.parametrize('field', ['calibration_id', 'uncertainty_m'])
def test_other_required_scene_null_is_not_exempted(gateway, field):
    config, inspect = gateway
    config['scene']['geometry'][field] = None
    row = inspect()
    assert not row['ready']
    assert 'unfilled:.scene.geometry.' + field in row['reasons']


def test_optional_occlusion_does_not_exempt_unverified_clock(gateway):
    config, inspect = gateway
    config['observation']['source_clock_verified'] = False
    row = inspect()
    assert not row['ready']
    assert 'source_clock_unverified' in row['reasons']


@pytest.mark.parametrize('invalid', [False, 'disabled', []])
def test_only_null_disables_occlusion(gateway, invalid):
    config, inspect = gateway
    config['scene']['geometry']['fixture_occlusion'] = invalid
    row = inspect()
    assert not row['ready']
    assert 'invalid_fixture_occlusion_configuration' in row['reasons']

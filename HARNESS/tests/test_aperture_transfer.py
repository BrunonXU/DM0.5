from types import SimpleNamespace

import numpy as np
import pytest

from f1_harness.aperture_transfer import _registered_corners, measure_observation


def shapes(scale=1., offset=(0.,0.)):
    def point(p):
        return (scale*np.asarray(p)+offset).tolist()
    return dict(triangle=[dict(center_px=point([20,20]), corners_px=point([[10,10],[35,20],[15,30]]))],
                square=[dict(center_px=point([42,50]), corners_px=point([[38,46],[46,46],[46,54],[38,54]]))],
                circle=[dict(center_px=point([20,42]), ellipse=[point([20,42]),[scale*12,scale*8],13])])


def test_observed_square_transfers_when_occluded_in_the_reference_camera():
    left, head = shapes(), shapes(1.4,(31,9))
    expected=head.pop('square')[0]
    result, = _registered_corners(left,head,.3)
    assert np.allclose(result[0],expected['corners_px'])
    assert result[-1] < 1e-8


@pytest.mark.parametrize('ambiguous', [False, True])
def test_identify_by_metric_shape_and_spacing_after_bucket_translation(monkeypatch, ambiguous):
    from f1_harness import aperture_transfer
    registered = shapes()
    current = shapes(offset=(35, 15))
    left = dict(current, square=list(current['square']))
    if ambiguous:
        left['square'].append(left['square'][0])
    else:
        left['square'].append(dict(center_px=[66,69], corners_px=[[64,65],[68,65],[68,73],[64,73]]))
    camera=dict(k_rgb=[[1000.,0,60],[0,1000.,60],[0,0,1]],roi=[0,0,120,120],base_camera=np.eye(4).tolist())
    nominal=np.eye(4)
    nominal[:3,3]=[-.018,-.010,1.]
    config=dict(base_hole=nominal,fixture_occlusion=dict(anchors={'bucket-triangle':dict(
        roi_center_px=registered['triangle'][0]['center_px'],depth_m=1.)}),
        cross_view_aperture=dict(inner_dimensions_m=[.008,.008]),hole_clearance_m=.0015,planning_margin_m=.015)
    monkeypatch.setattr(aperture_transfer,'aperture_features',lambda view:view)
    args=(left,current,np.ones((120,120)),camera,np.eye(4),config)
    if ambiguous:
        with pytest.raises(ValueError,match='ambiguous'):
            aperture_transfer.measure_aperture(*args)
    else:
        pose,_=aperture_transfer.measure_aperture(*args)
        assert np.allclose(pose[:3,3]-nominal[:3,3],[.035,.015,0])


def test_optional_left_source_and_skew_checked_before_decoding():
    scene=SimpleNamespace(config=dict(hole_camera=dict(camera='head'),cross_view_aperture=dict(max_pair_skew_ns=50)))
    def frame(camera,stream,stamp):
        return SimpleNamespace(camera=camera,stream=stream,received_ns=stamp,source_ns=stamp,
                               frame_id=camera+'_wrist_color_optical_frame',camera_info_sha256='camera-info')
    head=[frame('head','rgb',120),frame('head','depth',121)]
    with pytest.raises(ValueError,match='frames_missing'):
        measure_observation(scene,SimpleNamespace(frames=head),None)
    with pytest.raises(ValueError,match='not_contemporaneous'):
        measure_observation(scene,SimpleNamespace(frames=head+[frame('left','rgb',69)]),None)

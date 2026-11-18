"""Displayed RGB prompts never become geometry or motion authority."""
import numpy as np
import pytest

from f1_harness.target_relocalization import source_search_box, rectified_search_box


def metadata(camera='head', *, roi=(307,28,1229,720), size=(1280,720), display=(922,692)):
    return dict(camera=camera, observation_id='observed-image', image_id='issued-1',
        acquisition_instance='capture-1', scope={'epoch':3},
        frame=dict(camera=camera,stream='rgb',width=size[0],height=size[1],
                   blob_sha256='a'*64,camera_info_sha256='b'*64,sequence=23,source_ns=100),
        roi=None if roi is None else list(roi),source_image_size=list(size),model_image_size=list(display))


def config(camera='head', *, roi=(307,28,1229,720), distortion=(0.,0.,0.,0.,0.)):
    return dict(camera=camera,roi=list(roi),k_rgb=[[600.,0.,640.],[0.,600.,360.],[0.,0.,1.]],
                d_rgb=list(distortion),rgb_info_sha256='b'*64)


def test_head_display_roi_and_full_source_identity():
    meta=metadata();hint=source_search_box(meta,[0.,0.,1.,1.])
    assert hint['box_source_px']==[307.,28.,1229.,720.]
    assert hint['frame']==meta['frame'] and hint['scope']=={'epoch':3}
    assert hint['image_id']=='issued-1' and hint['observation_id']=='observed-image'
    result=rectified_search_box(hint,config())
    assert result['box']==[0,0,922,692]
    assert result['search_only'] and not result['motion_evidence']
    assert 'pose' not in result and 'mask' not in result
    result['frame']['sequence']=500
    assert meta['frame']['sequence']==hint['frame']['sequence']==23


def test_normalized_hint_scales_after_crop_and_ignores_thumbnail_resolution():
    box=[.25,.125,.75,.875]
    a=source_search_box(metadata(display=(461,346)),box)
    b=source_search_box(metadata(display=(922,692)),box)
    assert a['box_source_px']==b['box_source_px']==[537.5,114.5,998.5,633.5]
    assert rectified_search_box(a,config())['box']==[230,86,692,606]


def test_right_full_image_and_registered_crop_clipping():
    hint=source_search_box(metadata('right',roi=None,size=(640,480),display=(320,240)),[.1,.1,.8,.8])
    assert hint['box_source_px']==pytest.approx([64,48,512,384])
    result=rectified_search_box(hint,config('right',roi=(100,100,600,400)))
    assert result['box']==[0,0,412,284] and result['clipped_to_roi']


@pytest.mark.parametrize('bounds',[[0,0,0,1],[1,0,.5,1],[-.01,0,1,1],[0,0,1,1.01],
    [0,0,float('nan'),1],[0,0,float('inf'),1],[False,0,1,1],['0',0,1,1],[0,0,1]])
def test_invalid_normalized_bounds_rejected(bounds):
    with pytest.raises(ValueError,match='normalized_box_invalid'):
        source_search_box(metadata(),bounds)


@pytest.mark.parametrize('change,reason',[
    ({'source_image_size':[640,480]},'source_frame_size_mismatch'),
    ({'model_image_size':[0,692]},'model_size_invalid'),
    ({'roi':[307,28,1400,720]},'display_roi_invalid'),
    ({'roi':[307.5,28,1229,720]},'display_roi_invalid'),
    ({'camera':'left'},'rgb_camera_mismatch'),
    ({'observation_id':''},'observation_identity_required')])
def test_invalid_image_metadata_rejected(change,reason):
    with pytest.raises(ValueError,match=reason):
        source_search_box(dict(metadata(),**change),[0,0,1,1])


def test_region_outside_registered_roi_is_not_retargeted_to_roi_edge():
    hint=source_search_box(metadata(roi=None,display=(640,360)),[0,0,.1,.1])
    with pytest.raises(ValueError,match='outside_registered_roi'):
        rectified_search_box(hint,config())


def test_view_or_camera_identity_change_rejected():
    hint=source_search_box(metadata(),[.2,.2,.5,.5])
    with pytest.raises(ValueError,match='view_camera_mismatch'):
        rectified_search_box(hint,config('right'))
    with pytest.raises(ValueError,match='camera_info_changed'):
        rectified_search_box(hint,dict(config(),rgb_info_sha256='c'*64))


def test_distorted_box_uses_perimeter_extrema_and_matches_rectification_map():
    import cv2
    # Positive radial distortion bends horizontal edges; the topmost corrected
    # midpoint lies beyond the rectangle obtained from only four corners.
    cfg=dict(config(roi=(0,0,1280,720),distortion=(.45,.08,0.,0.,0.)),
             k_rgb=[[500.,0.,640.],[0.,500.,360.],[0.,0.,1.]])
    hint=source_search_box(metadata(roi=None,display=(640,360)),[.25,.1,.75,.35])
    result=rectified_search_box(hint,cfg)
    k=np.asarray(cfg['k_rgb']);d=np.asarray(cfg['d_rgb'])
    bounds=np.array(hint['box_source_px']);lo,hi=bounds[:2],bounds[2:]
    corners=np.array([[lo[0],lo[1]],[hi[0],lo[1]],[lo[0],hi[1]],[hi[0],hi[1]]])
    four=cv2.undistortPoints(corners.reshape(-1,1,2),k,d,P=k).reshape(-1,2)
    assert result['box'][1] < np.floor(four[:,1].min())
    # The existing remap maps rectified pixels back into raw source pixels.
    raw=np.array([640.,bounds[1]])
    corrected=cv2.undistortPoints(raw.reshape(1,1,2),k,d,P=k).reshape(2)
    maps=cv2.initUndistortRectifyMap(k,d,None,k,(1280,720),cv2.CV_32FC1)
    x,y=np.round(corrected).astype(int)
    assert np.allclose([maps[0][y,x],maps[1][y,x]],raw,atol=1.)
    x0,y0,x1,y1=result['box']
    assert x0<=corrected[0]<=x1 and y0<=corrected[1]<=y1
    assert result['perimeter_samples']>4


@pytest.mark.parametrize('change',[{'d_rgb':[0.,0.]},{'d_rgb':[0.,float('nan'),0.,0.]},
    {'k_rgb':[[0,0,640],[0,600,360],[0,0,1]]},{'roi':[0,0,0,720]}])
def test_invalid_rectification_configuration_rejected(change):
    hint=source_search_box(metadata(),[.2,.2,.5,.5])
    with pytest.raises(ValueError):rectified_search_box(hint,dict(config(),**change))

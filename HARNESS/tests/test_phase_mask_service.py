import numpy as np
import pytest

from f1_harness.mask_service import MaskClient,mask_server


def test_overlap_is_forwarded_only_when_explicitly_delegated_to_scene_validation():
    class Segmenter:
        def segment(self,rgb,box):
            mask=np.zeros(rgb.shape[:2],bool);mask[2:5,3:7]=True
            return [(mask,.95)]
    cfg=dict(port=0,prompts={'block':[1,1,8,8],'hole':[1,1,8,8]},image_shape=[10,10,3],minimum_score=.9,
             minimum_area_px=4,prompt_padding_px=1,maximum_overlap_fraction=.1)
    for forwarded in (False,True):
        cfg['scene_validated_overlap_pairs']=[['block','hole']] if forwarded else []
        server=mask_server(cfg,segmenter=Segmenter());client=MaskClient(f'http://127.0.0.1:{server.server_address[1]}')
        try:
            if forwarded:
                masks=client(np.zeros((10,10,3),np.uint8))
                assert np.array_equal(masks['block'],masks['hole'])
            else:
                with pytest.raises(ValueError,match='rejected'):client(np.zeros((10,10,3),np.uint8))
        finally:client.close();server.shutdown();server.server_close()


def test_fixed_color_service_reports_missing_optional_hole_without_inventing_mask():
    cfg=dict(backend='fixed_color',port=0,prompts={'block':[1,1,29,29],'hole':[1,1,29,29]},
             image_shape=[30,30,3],minimum_score=.9,minimum_area_px=20,prompt_padding_px=1,
             maximum_overlap_fraction=.05,optional_ids=['hole'],
             color_profiles={'block':dict(hsv_lower=[0,200,200],hsv_upper=[5,255,255],minimum_area_px=20,maximum_area_px=50),
                             'hole':dict(hsv_lower=[50,200,200],hsv_upper=[70,255,255],minimum_area_px=20,maximum_area_px=50)})
    server=mask_server(cfg);client=MaskClient(f'http://127.0.0.1:{server.server_address[1]}')
    try:
        image=np.zeros((30,30,3),np.uint8);image[3:8,4:9]=[255,0,0]
        masks=client(image)
        assert set(masks)=={'block'} and masks['block'].sum()==25
        with pytest.raises(ValueError,match='rejected'):client(np.zeros_like(image))
    finally:client.close();server.shutdown();server.server_close()

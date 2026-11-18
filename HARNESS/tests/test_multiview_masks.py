import numpy as np
import pytest

from f1_harness.mask_service import MaskClient,mask_server


def view(shape,object_id,hue):
    return dict(backend='fixed_color',prompts={object_id:[1,1,shape[1]-1,shape[0]-1]},
        image_shape=shape,minimum_score=.9,minimum_area_px=20,prompt_padding_px=1,
        maximum_overlap_fraction=.05,optional_ids=[],color_profiles={object_id:dict(
            hsv_lower=[hue,200,200],hsv_upper=[hue+5,255,255],minimum_area_px=20,maximum_area_px=60)})


def test_named_views_use_independent_dimensions_prompts_and_identity():
    config=dict(port=0,views={'head':view([30,40,3],'hole',0),
                              'right':view([50,60,3],'block',60)})
    server=mask_server(config);client=MaskClient(f'http://127.0.0.1:{server.server_address[1]}')
    head=np.zeros((30,40,3),np.uint8);head[5:10,6:11]=[255,0,0]
    right=np.zeros((50,60,3),np.uint8);right[7:12,8:13]=[0,255,0]
    try:
        assert set(client(head,view='head'))=={'hole'}
        assert set(client(right,view='right'))=={'block'}
        with pytest.raises(ValueError,match='rejected'):client(head,view='right')
        with pytest.raises(ValueError,match='rejected'):client(head,view='unknown')
        with pytest.raises(ValueError,match='rejected'):client(head)
        assert set(client(head,view='head'))=={'hole'}
    finally:client.close();server.shutdown();server.server_close()


def test_worker_rejection_preserves_view_object_and_actual_gate():
    server=mask_server(dict(port=0,views={'right':view([30,40,3],'block',0)}))
    client=MaskClient(f'http://127.0.0.1:{server.server_address[1]}')
    try:
        with pytest.raises(ValueError) as rejected:
            client(np.zeros((30,40,3),np.uint8),view='right')
        error=str(rejected.value)
        assert 'mask_uncertain_requires_new_prompt' in error
        assert '"view": "right"' in error and '"object_id": "block"' in error
        with pytest.raises(ValueError,match='ROI_image_contract'):
            client(np.zeros((20,40,3),np.uint8),view='right')
    finally:client.close();server.shutdown();server.server_close()


def test_explicit_default_and_client_bound_view():
    config=dict(port=0,default_view='head',views={'head':view([30,40,3],'hole',0),
                                                'right':view([30,40,3],'block',60)})
    server=mask_server(config);url=f'http://127.0.0.1:{server.server_address[1]}'
    default=MaskClient(url);bound=MaskClient(url,view='right')
    red=np.zeros((30,40,3),np.uint8);red[5:10,6:11]=[255,0,0]
    green=red.copy();green[5:10,6:11]=[0,255,0]
    try:
        assert set(default(red))=={'hole'}
        assert set(bound(green))=={'block'}
        with pytest.raises(ValueError,match='rejected'):bound(red)
        with pytest.raises(ValueError,match='invalid_mask_view'):bound(green,view='../head')
    finally:default.close();bound.close();server.shutdown();server.server_close()


def test_legacy_single_view_has_no_header_requirement():
    cfg=view([30,40,3],'block',0);cfg['port']=0
    server=mask_server(cfg);client=MaskClient(f'http://127.0.0.1:{server.server_address[1]}')
    image=np.zeros((30,40,3),np.uint8);image[5:10,6:11]=[255,0,0]
    try:assert set(client(image))=={'block'}
    finally:client.close();server.shutdown();server.server_close()


@pytest.mark.parametrize('views,default',[(None,None),({},None),({'../head':{}},None),({'head':{}},'missing')])
def test_invalid_view_registry_rejected_before_opening_socket(views,default):
    with pytest.raises(ValueError):mask_server(dict(port=0,views=views,default_view=default))


def test_opted_in_color_tracking_moves_with_visible_object_and_never_guesses_missing():
    cfg=view([60,80,3],'block',0);cfg.update(port=0,fixed_color_tracking_padding_px=20)
    cfg['prompts']['block']=[5,5,22,30]
    server=mask_server(cfg);client=MaskClient(f'http://127.0.0.1:{server.server_address[1]}')
    try:
        for x in (10,20,30,40):
            image=np.zeros((60,80,3),np.uint8);image[12:17,x:x+5]=[255,0,0]
            mask=client(image)['block']
            assert np.nonzero(mask)[1].min()==x
        with pytest.raises(ValueError,match='rejected'):client(np.zeros_like(image))
        assert np.array_equal(client(image)['block'],mask)
    finally:client.close();server.shutdown();server.server_close()


@pytest.mark.parametrize('padding',[True,0,-1,257,2.5])
def test_invalid_color_tracking_configuration_rejected(padding):
    cfg=view([30,40,3],'block',0);cfg.update(port=0,fixed_color_tracking_padding_px=padding)
    with pytest.raises(ValueError,match='tracking_padding'):mask_server(cfg)


def test_fixture_depth_region_is_visible_lid_ring_not_hole_interior():
    cfg=view([40,50,3],'anchor',0);cfg.update(port=0,mask_regions={'anchor':dict(kind='outer_ring',inner_px=1,outer_px=3)})
    server=mask_server(cfg);client=MaskClient(f'http://127.0.0.1:{server.server_address[1]}')
    image=np.zeros((40,50,3),np.uint8);image[15:20,20:25]=[255,0,0]
    try:
        ring=client(image)['anchor']
        assert not ring[15:20,20:25].any()
        assert ring[12,17] and ring[22,27]
        with pytest.raises(ValueError,match='rejected'):client(np.zeros_like(image))
    finally:client.close();server.shutdown();server.server_close()


def test_ring_never_invents_missing_anchor_and_rejects_unbounded_growth():
    from f1_harness.perception import measurement_region
    assert not measurement_region(np.zeros((20,20),bool),dict(kind='outer_ring',inner_px=1,outer_px=3)).any()
    with pytest.raises(ValueError,match='bounded'):
        measurement_region(np.zeros((20,20),bool),dict(kind='outer_ring',inner_px=1,outer_px=100))

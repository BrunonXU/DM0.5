"""Native fixed-color execution uses the reviewed HTTP algorithm and identity."""
import hashlib
import json

import numpy as np
import pytest

from f1_harness.mask_service import ConfiguredMaskProvider, MaskClient, mask_server, scene_mask_provider
from test_multiview_masks import view


def configuration():
    return dict(port=0,views={'head':view([30,40,3],'hole',0),'right':view([50,60,3],'block',60)})


def reviewed(tmp_path,config):
    data=json.dumps(config).encode()
    (tmp_path/'masks.local.json').write_bytes(data)
    (tmp_path/'development.local.json').write_text(json.dumps(
        {'reviewed_files':{'masks.local.json':hashlib.sha256(data).hexdigest()}}))
    return dict(mask_execution='local_fixed_color',local_mask_config='masks.local.json')


def test_native_and_http_providers_have_identical_masks(tmp_path):
    cfg=configuration()
    local=scene_mask_provider(reviewed(tmp_path,cfg),tmp_path)
    server=mask_server(cfg);remote=MaskClient(f'http://127.0.0.1:{server.server_address[1]}')
    try:
        for camera,shape,color in [('head',(30,40,3),[255,0,0]),('right',(50,60,3),[0,255,0])]:
            pixels=np.zeros(shape,np.uint8);pixels[5:10,6:11]=color
            lhs,rhs=local(pixels,view=camera),remote(pixels,view=camera)
            assert lhs.keys()==rhs.keys()
            assert all(np.array_equal(lhs[key],rhs[key]) for key in lhs)
    finally:local.close();remote.close();server.shutdown();server.server_close()


def test_local_masks_require_content_identity(tmp_path):
    scene=reviewed(tmp_path,configuration())
    (tmp_path/'masks.local.json').write_text('{}')
    with pytest.raises(ValueError,match='identity_changed'):scene_mask_provider(scene,tmp_path)


def test_local_masks_reject_symlinks(tmp_path):
    scene=reviewed(tmp_path,configuration())
    (tmp_path/'masks.local.json').rename(tmp_path/'other.json')
    (tmp_path/'masks.local.json').symlink_to(tmp_path/'other.json')
    with pytest.raises(ValueError,match='regular_reviewed'):scene_mask_provider(scene,tmp_path)


@pytest.mark.parametrize('backend',[None,'sam2','auto','unknown'])
def test_local_provider_has_no_implicit_model_fallback(backend):
    cfg=configuration()
    if backend is None:cfg['views']['head'].pop('backend')
    else:cfg['views']['head']['backend']=backend
    with pytest.raises(ValueError,match='explicit_fixed_color'):
        ConfiguredMaskProvider(cfg,fixed_only=True)


def test_unknown_execution_mode_cannot_fallback_to_network(tmp_path):
    with pytest.raises(ValueError,match='unknown_mask_execution'):
        scene_mask_provider({'mask_execution':'automatic'},tmp_path)

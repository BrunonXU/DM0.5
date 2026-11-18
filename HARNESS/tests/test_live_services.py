import numpy as np
import pytest
from f1_harness.evidence import FrameCache
from f1_harness.mask_service import MaskClient,mask_server


def test_frame_cache_is_bounded_and_reports_eviction():
    cache=FrameCache(10)
    old=cache.put_blob(b'12345678')
    new=cache.put_blob(b'abcdefgh')
    assert cache.size==8 and cache.blob(new)==b'abcdefgh'
    with pytest.raises(ValueError,match='evicted'):cache.blob(old)


def test_mask_transport_is_separate_binary_channel():
    class Segmenter:
        def segment(self,rgb,box):
            mask=np.zeros(rgb.shape[:2],bool);mask[2:5,3:7]=True
            return [(mask,.95)]
    config=dict(port=0,prompts={'block':[1,1,8,8]},image_shape=[10,10,3],minimum_score=.9,
                minimum_area_px=4,prompt_padding_px=1,maximum_overlap_fraction=.1)
    server=mask_server(config,segmenter=Segmenter())
    client=MaskClient(f'http://127.0.0.1:{server.server_address[1]}')
    try:
        assert client(np.zeros((10,10,3),np.uint8))['block'].sum()==12
        with pytest.raises(ValueError,match='rejected'):client(np.zeros((9,9,3),np.uint8))
    finally:client.close();server.shutdown();server.server_close()


def test_service_config_is_not_planner_config(tmp_path,monkeypatch):
    from f1_harness import services
    from f1_harness.cli import main
    config=tmp_path/'service.json';config.write_text('{}')
    seen=[]
    def serve(kind,path,store):
        seen.append((kind,path));return {'ready':True}
    monkeypatch.setattr(services,'serve',serve)
    assert main(['--run',str(tmp_path/'run'),'serve','gateway',str(config)])==0
    assert seen==[('gateway',config)]

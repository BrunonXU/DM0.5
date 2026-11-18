"""Fixture recovery keeps image identity, grasp proof and batch mask identities."""
from copy import deepcopy
import io
import json
import httpx
import numpy as np
import pytest

from f1_harness.mask_service import ConfiguredMaskProvider
from f1_harness.perception import validate_point_prompt
from f1_harness.sam_service import RemoteSam2Segmenter, validate_batch_prompts
from f1_harness.target_relocalization import source_search_box, rectified_search_box
from test_sam_runtime import sam_config
from test_target_relocalization import metadata, config as camera_config


@pytest.mark.parametrize('value',[
    {'points':[[1,2]],'labels':[2]}, {'points':[[1,2]],'labels':[.5]},
    {'points':[[float('nan'),2]],'labels':[1]}, {'points':[[80,2]],'labels':[1]},
    {'points':[[1,-1]],'labels':[0]}, {'points':[[1,2]],'labels':[]},
    {'points':[],'labels':[]}, {'points':[[1,2]]},
])
def test_invalid_points_do_not_partially_change_prompt(value):
    provider=ConfiguredMaskProvider(sam_config(),segmenter={'right':object()})
    before=deepcopy(provider.tracking_snapshot())
    with pytest.raises(ValueError):
        provider.set_search_hint(view='right',object_id='block',box=[10,10,40,40],point_prompt=value)
    assert provider.tracking_snapshot()==before
    assert provider.states['right']['point_seeds']=={}


def test_points_follow_actual_crop_and_rectification_not_thumbnail_size():
    hint=source_search_box(metadata(display=(461,346)),[.2,.2,.8,.8],
        points_normalized=[[.4,.4],[.5,.5]],point_labels=[1,0])
    assert np.allclose(hint['points_source_px'],[[675.8,304.8],[768,374]])
    mapped=rectified_search_box(hint,camera_config())
    assert np.allclose(mapped['point_prompt']['points'],[[368.8,276.8],[461,346]])
    assert mapped['point_prompt']['labels']==[1,0]
    assert not mapped['motion_evidence']


def fixture_config():
    cfg=sam_config();view=cfg['views']['right']
    view.update(prompts={'block':[20,20,40,40],'hole':[45,45,65,65]},
                sam_objects=['block','hole'],color_profiles={})
    return cfg


class BatchSam:
    def __init__(self):self.batches=[];self.single=[];self.tracks=[]
    def result(self,box,score=.99):
        mask=np.zeros((80,80),bool)
        if box[0]<40:mask[25:35,25:35]=True
        else:mask[50:60,50:60]=True
        return mask,score
    def segment_many(self,rgb,*,prompts):
        self.batches.append(prompts)
        return {key:self.result(args['box']) for key,args in prompts.items()}
    def segment(self,rgb,*,box,**kwargs):
        self.single.append(box);return [self.result(box)]
    def track(self,rgb,*,box,**kwargs):
        self.tracks.append(kwargs)
        return self.result(box),dict(score_kind='object_presence_not_mask_iou',motion_evidence=False)


def test_fixture_batch_keeps_positive_negative_points_and_entity_masks():
    sam=BatchSam();provider=ConfiguredMaskProvider(fixture_config(),segmenter={'right':sam})
    provider.set_search_hint(view='right',object_id='hole',box=[45,45,65,65],
        point_prompt={'points':[[55,55],[47,47]],'labels':[1,0]})
    masks=provider(np.zeros((80,80,3),np.uint8),view='right')
    assert len(sam.batches)==1 and not sam.single
    assert masks['block'][30,30] and masks['hole'][55,55]
    assert sam.batches[0]['hole']['labels'].tolist()==[1,0]
    assert sam.batches[0]['hole']['points'].tolist()==[[55,55],[47,47]]


def test_only_moving_object_uses_video_memory():
    cfg=fixture_config();cfg['views']['right'].update(sam_streaming=True,sam_streaming_objects=['block'])
    sam=BatchSam();provider=ConfiguredMaskProvider(cfg,segmenter={'right':sam})
    masks=provider.segment_frame(np.zeros((80,80,3),np.uint8),view='right',
        source={'source_ns':4,'stream':{'camera':'right'}})
    assert set(masks)=={'block','hole'}
    assert [call['object_id'] for call in sam.tracks]==['block']
    assert len(sam.single)==1


def test_low_quality_batch_mask_is_advisory_without_extra_inference():
    cfg=fixture_config();cfg['views']['right']['reacquisition_boxes']={'hole':[42,42,68,68]}
    class Recover(BatchSam):
        def segment_many(self,*args,**kwargs):
            result=super().segment_many(*args,**kwargs)
            result['hole']=(result['hole'][0],.1)
            return result
    sam=Recover();provider=ConfiguredMaskProvider(cfg,segmenter={'right':sam})
    assert len(provider(np.zeros((80,80,3),np.uint8),view='right'))==2
    assert len(sam.batches)==1 and len(sam.single)==0
    assert provider.diagnostic_snapshot()['right']['hole']['quality_warning']=='mask_quality_below_reference'


def test_outer_ring_does_not_expand_future_segmentation_prompt():
    cfg=fixture_config();cfg['views']['right']['mask_regions']={'hole':{'kind':'outer_ring','inner_px':0,'outer_px':3}}
    sam=BatchSam();provider=ConfiguredMaskProvider(cfg,segmenter={'right':sam})
    provider(np.zeros((80,80,3),np.uint8),view='right')
    assert provider.states['right']['visual_prompts']['hole']==[42,42,68,68]


@pytest.mark.parametrize('same_identity_pixels', [False, True])
def test_identity_overlap_uses_segmentation_and_returns_depth_ring(same_identity_pixels):
    cfg=fixture_config()
    cfg['views']['right'].update(optional_ids=['block','hole'],
        mask_regions={'hole':{'kind':'outer_ring','inner_px':3,'outer_px':8}})
    hole=np.zeros((80,80),bool)
    hole[30:40,30:40]=True
    block=hole.copy() if same_identity_pixels else np.zeros_like(hole)
    if not same_identity_pixels:
        block[30:40,43:48]=True

    class RingSam(BatchSam):
        def segment_many(self,rgb,*,prompts):
            return {'block':(block,.99),'hole':(hole,.99)}

    provider=ConfiguredMaskProvider(cfg,segmenter={'right':RingSam()})
    masks=provider(np.zeros((80,80,3),np.uint8),view='right')
    if same_identity_pixels:
        assert masks=={}
        assert provider.diagnostic_snapshot()['right']['block']['identity_overlaps']==[dict(
            other='hole',shared_segmentation_px=100,smaller_area_px=100,fraction=1.)]
    else:
        assert set(masks)=={'hole','block'}
        assert not (masks['hole'] & hole).any()
        assert (masks['hole'] & block).any()
        assert np.array_equal(masks['block'],block)


def test_lid_context_overlap_does_not_freeze_aperture_search():
    cfg=fixture_config();view=cfg['views']['right']
    view['prompts']['lid']=[40,40,75,75];view['sam_objects'].append('lid')
    view['context_region_ids']=['lid']
    class Lid(BatchSam):
        def segment_many(self,*args,**kwargs):
            result=super().segment_many(*args,**kwargs)
            mask=np.zeros((80,80),bool);mask[42:73,42:73]=True
            result['lid']=(mask,.99)
            return result
    sam=Lid();provider=ConfiguredMaskProvider(cfg,segmenter={'right':sam})
    provider(np.zeros((80,80,3),np.uint8),view='right')
    assert set(provider.states['right']['visual_prompts'])=={'block','hole','lid'}
    assert provider.states['right']['visual_prompts']['hole']==[42,42,68,68]


def test_all_sam_fixture_group_is_supported_but_mixed_group_is_rejected():
    cfg=fixture_config();cfg['views']['right']['fixture_tracking_group']={'primary':'hole','members':['hole','block']}
    sam=BatchSam();provider=ConfiguredMaskProvider(cfg,segmenter={'right':sam})
    assert len(provider(np.zeros((80,80,3),np.uint8),view='right'))==2
    cfg['views']['right']['sam_objects']=['block']
    cfg['views']['right']['color_profiles']={'hole':{}}
    with pytest.raises(ValueError,match='mixed_fixture_tracking_backends'):
        ConfiguredMaskProvider(cfg,segmenter={'right':sam})


@pytest.mark.parametrize('mutate',[
    lambda x:x.append(x[0]), lambda x:x[0].update(box=[0,0,90,90]),
    lambda x:x[0].update(point_prompt={'points':[[5,5]],'labels':[3]}),
])
def test_batch_validates_every_prompt_before_inference(mutate):
    values=[dict(object_id='hole',box=[1,1,10,10])];mutate(values)
    with pytest.raises(ValueError):validate_batch_prompts(values,(80,80))


@pytest.mark.parametrize('wrong_identity',[False,True])
def test_batch_http_contract_preserves_order_and_one_image_upload(wrong_identity):
    calls=[]
    def handler(request):
        calls.append(request)
        prompts=json.loads(request.headers['X-SAM-Prompts'])
        assert [x['object_id'] for x in prompts]==['hole','lid']
        assert prompts[0]['point_prompt']=={'points':[[5,5]],'labels':[1]}
        stream=io.BytesIO();np.savez_compressed(stream,masks=np.zeros((2,80,80),bool),scores=[.9,.95])
        return httpx.Response(200,content=stream.getvalue(),headers={
            'X-SAM-Image':request.headers['X-SAM-Image'],'X-SAM-Model':'a'*64,
            'X-SAM-Entities':json.dumps(['lid','hole'] if wrong_identity else ['hole','lid'])})
    remote=RemoteSam2Segmenter('http://127.0.0.1:1',model_sha256='a'*64)
    remote.client.close();remote.client=httpx.Client(transport=httpx.MockTransport(handler))
    args=dict(prompts={'hole':dict(box=[1,1,10,10],points=[[5,5]],labels=[1]),'lid':dict(box=[2,2,30,30])})
    try:
        if wrong_identity:
            with pytest.raises(ValueError,match='entities_changed'):remote.segment_many(np.zeros((80,80,3),np.uint8),**args)
        else:
            result=remote.segment_many(np.zeros((80,80,3),np.uint8),**args)
            assert result['hole'][1]==.9 and result['lid'][1]==.95
        assert len(calls)==1
    finally:remote.close()

@pytest.mark.parametrize('optional,expected', [(['block'], {'hole'}), (['block','hole'], set()), ([], None)])
def test_ambiguous_optional_identity_does_not_reject_independent_masks(optional,expected):
    cfg=fixture_config();cfg['views']['right']['optional_ids']=optional
    class Overlap(BatchSam):
        def segment_many(self,rgb,*,prompts):
            self.batches.append(prompts)
            return {key:self.result([20,20,40,40]) for key in prompts}
    provider=ConfiguredMaskProvider(cfg,segmenter={'right':Overlap()})
    if expected is None:
        with pytest.raises(ValueError,match='object_identity_overlap'):
            provider(np.zeros((80,80,3),np.uint8),view='right')
    else:
        assert set(provider(np.zeros((80,80,3),np.uint8),view='right'))==expected
        for key in optional:
            assert provider.diagnostic_snapshot()['right'][key]['rejected_reason']=='object_identity_overlap'

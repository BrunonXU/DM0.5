"""Structured unknowns and seed identities survive ordinary scene updates."""
import numpy as np

from f1_harness.geometry_context import entity_status
from f1_harness.mask_service import ConfiguredMaskProvider
from test_sam_runtime import Sam, sam_config
from test_independent_fixture import coarse_case


def test_seed_provenance_persists_only_within_the_original_stream():
    cfg=sam_config();cfg['views']['right']['sam_streaming']=True
    class Video(Sam):
        def track(self,rgb,**kw):
            mask,_=self.segment(rgb,box=kw['box'])[0]
            metadata=dict(score_kind='object_presence_not_mask_iou',
                stream_id=kw['stream_id'],source_ns=kw['source_ns'],reseeded=kw['source_ns']==50)
            if metadata['reseeded']:
                metadata.update(seed_backend='sam3',seed_reason='unique_in_current_hint',
                                semantic_model_sha256='a'*64)
            return (mask,.75),metadata
    provider=ConfiguredMaskProvider(cfg,segmenter={'right':Video()})
    source=dict(source_ns=50,stream={'acquisition':'a','camera':'right','epoch':1})
    rgb=np.zeros((80,80,3),np.uint8)
    def sample(source):
        provider.segment_frame(rgb,view='right',source=source)
        return provider.diagnostic_snapshot()['right']['block']['tracking']
    first=sample(source)
    second=sample(dict(source,source_ns=51))
    assert second['source_ns']==51 and second['seed_provenance']==first['seed_provenance']
    assert second['seed_provenance']['source_ns']==50
    assert second['seed_provenance']['seed_backend']=='sam3'
    changed=sample(dict(source,source_ns=52,stream={'acquisition':'b','camera':'right','epoch':1}))
    assert changed.get('seed_provenance') is None
    provider.reset_visual_tracking()
    assert sample(dict(source,source_ns=53)).get('seed_provenance') is None


def test_unknown_attachment_is_not_empty_or_a_pose_certificate():
    service,obs,_,_=coarse_case()
    obj=obs.objects[0].model_copy(update=dict(held=None,eef_object_pose=None,evidence_ids=('source-frame',)))
    obs=obs.model_copy(update=dict(objects=(obj,)))
    status=entity_status(service.task,obs)
    row=status['entities'][0]
    assert row['held'] is None and row['evidence_ids']==['source-frame']
    assert row['in_hand_transform_available'] is False
    assert status['measurement_received_ns']==obs.received_ns
    assert status['perception_boundary']['shape_label_is_independent_classification'] is False
    # Actual geometry_context caller adds its own observation identity. No
    # duplicate keyword or promotion of the bucket reference to a hole pose.
    context=service.dispatch('geometry_context',dict(observation=obs.model_dump(),
        full22=[0.]*22,held16=list(obs.state16)))
    assert context['observation_id']==obs.observation_id
    assert context['hole_pose'] is None
    assert context['entities'][0]['held'] is None

"""Actual smoother state, fake scene verdicts; no production command owner."""
import importlib.util,sys,unittest
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
from dataclasses import asdict,replace
import numpy as np
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from test_segment_candidates_native import SegmentCandidateTests
from consume_simulated_segments import SimulatedSegmentChoices
from segment_candidates import recommend_segment,RecommendationInvalidated,state_binding
from f1_harness.trajectory import schedule,sample
from f1_harness.contracts import digest
from f1_harness.owner_bridge import JointProposal


@unittest.skipUnless(importlib.util.find_spec('ruckig') is not None,'requires native environment')
class ConsumptionTests(unittest.TestCase):
    def fixture(self):
        output,ctx,args=SegmentCandidateTests().fixture();kwargs=args['forecast_kwargs']
        kwargs['trajectory']=schedule(kwargs['targets'],kwargs['trajectory']['anchor'],args['geometry_trace'],60.)
        proposal=JointProposal(request_id=ctx['request_id'],epoch=ctx['epoch'],scene_version='scene',calibration_id='c',
            full_reference=tuple(ctx['full_reference']),targets=tuple(tuple(v) for v in kwargs['targets']),
            tolerance_deg=None,settle_ns=300000000,gripper_tolerance=.5,
            geometry_trace=tuple(args['geometry_trace']),trajectory=kwargs['trajectory'])
        ctx['source_plan_sha256']=digest(asdict(proposal));args['context']=deepcopy(ctx)
        calls=[]
        def audit(f):
            result=SegmentCandidateTests.clear(f);calls.append(f)
            result[0]['complete']=True
            if len(calls)<3:result[0].update(passed=False,complete=False,failures=[dict(virtual_ns=1,fraction=1.)])
            return result
        recommendation=recommend_segment(output,**dict(args,audit=audit))
        assert recommendation['recommended_reference_speed']==40
        install=dict(output=output,context=ctx,current_context=lambda:ctx,cancelled=lambda:False,
            forecast_kwargs=kwargs,geometry_trace=args['geometry_trace'],reference_speeds=args['reference_speeds'],
            recommendation=recommendation)
        bridge=SimpleNamespace(engine=SimpleNamespace(mode=SimpleNamespace(value='fake'),epoch=ctx['epoch'],
            running=SimpleNamespace(request_id=ctx['request_id'],skill=kwargs['skill'])),
            proposal=proposal,clock=lambda:kwargs['first_publication_ns'])
        return output,ctx,kwargs,bridge,install

    def test_checked_segment_is_sampled_and_source_plan_and_state_unchanged(self):
        output,ctx,kw,b,args=self.fixture();before=state_binding(output,ctx);old=digest(asdict(b.proposal))
        choices=SimulatedSegmentChoices('fake');receipt=choices.install(**args)
        selected=choices.sample_for_bridge(b,kw['trajectory'],kw['targets'],start_index=0,elapsed_s=.04)
        original=sample(kw['trajectory'],kw['targets'],start_index=0,elapsed_s=.04)
        self.assertLess(selected[0][7],original[0][7]);self.assertEqual(selected[3],original[3])
        self.assertEqual(old,digest(asdict(b.proposal)));self.assertEqual(before,state_binding(output,ctx))
        self.assertFalse(receipt['production_authorized']);self.assertFalse(receipt['whole_skill_verified'])
        with self.assertRaisesRegex(ValueError,'already_installed'):choices.install(**args)

    def test_changed_control_state_or_epoch_cannot_consume_old_result(self):
        for field in ('epoch','velocity'):
            output,ctx,kw,b,args=self.fixture()
            if field=='epoch':ctx['epoch']+=1
            else:output.planner._radians.velocity[7]+=.01
            with self.assertRaises(RecommendationInvalidated):SimulatedSegmentChoices('fake').install(**args)

    def test_partial_clear_or_changed_digest_cannot_be_consumed(self):
        for field in ('samples','complete','plan','set'):
            output,ctx,kw,b,args=self.fixture();r=args['recommendation']
            if field=='samples':r['attempts'][-1]['audit'][0]['samples']-=1
            elif field=='complete':r['attempts'][-1]['audit'][0]['complete']=False
            elif field=='plan':r['attempts'][-1]['plan_sha256']='bad'
            else:r['candidate_set_sha256']='bad'
            with self.assertRaises(ValueError):SimulatedSegmentChoices('fake').install(**args)

    def test_contact_and_hardware_do_not_get_experimental_sampler(self):
        with self.assertRaisesRegex(ValueError,'fake_engine'):SimulatedSegmentChoices('hardware')
        output,ctx,kw,b,args=self.fixture();choices=SimulatedSegmentChoices('fake');choices.install(**args)
        b.engine.mode.value='hardware'
        with self.assertRaisesRegex(ValueError,'fake_engine'):
            choices.sample_for_bridge(b,kw['trajectory'],kw['targets'],start_index=0,elapsed_s=0)

    def test_next_segment_needs_new_state_and_new_choice(self):
        output,ctx,kw,b,args=self.fixture();choices=SimulatedSegmentChoices('fake');choices.install(**args)
        with self.assertRaisesRegex(ValueError,'missing_segment_choice'):
            choices.sample_for_bridge(b,kw['trajectory'],kw['targets'],start_index=1,elapsed_s=.02)

    def test_changed_plan_and_epoch_invalidate_sampling(self):
        for field in ('epoch','plan','horizon'):
            output,ctx,kw,b,args=self.fixture();choices=SimulatedSegmentChoices('fake');receipt=choices.install(**args)
            if field=='epoch':b.engine.epoch+=1
            elif field=='plan':b.proposal=replace(b.proposal,settle_ns=310000000)
            else:b.clock=lambda:receipt['conditional_until_ns']+1
            with self.assertRaises(RecommendationInvalidated):
                choices.sample_for_bridge(b,kw['trajectory'],kw['targets'],start_index=0,elapsed_s=.02)

    def test_cancellation_before_install_never_changes_sampler(self):
        output,ctx,kw,b,args=self.fixture();choices=SimulatedSegmentChoices('fake');args['cancelled']=lambda:True
        with self.assertRaises(RecommendationInvalidated):choices.install(**args)
        self.assertFalse(choices.choices)

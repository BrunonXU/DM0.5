"""Process-local recommendations cannot outlive their actual input state."""
import importlib.util
from pathlib import Path
import sys
import unittest
from copy import deepcopy
import numpy as np

ROOT=Path(__file__).parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import test_segment_preview_native as fixture_module
from segment_candidates import recommend_segment,state_binding,RecommendationInvalidated
from segment_preferences import propose_preference


@unittest.skipUnless(importlib.util.find_spec('ruckig') is not None,'requires native numerical environment')
class SegmentCandidateTests(unittest.TestCase):
    def fixture(self):
        output,kwargs=fixture_module.SegmentPreviewTests().fixture()
        context=dict(session_id='s',owner_instance_id='o',task_hash='t',epoch=1,calibration_id='c',
            observation=dict(scene_version='v',pose=[0.,1.,2.]),full_reference=[0.]*22,
            held_reference=kwargs['measured16'].tolist(),control_profile_sha256='p',
            request_id=kwargs['request_id'],source_plan_sha256='source-plan')
        args=dict(context=deepcopy(context),current_context=lambda:context,cancelled=lambda:False,
            forecast_kwargs=dict(kwargs,post_boundary_samples=40),
            geometry_trace=[dict(start_index=0,end_index=1),dict(start_index=1,end_index=2)],
            reference_speeds=[60,50,40],audit=self.clear)
        return output,context,args

    @staticmethod
    def clear(forecast):
        before=np.array(forecast['rows'][0]['measured16']);count=0
        for row in forecast['rows']:
            target=np.array(row['submitted16']);count+=max(1,int(np.ceil(max(abs(target[:14]-before[:14]))/.1)));before=target
        return [dict(skill=forecast['skill'],request_id=forecast['request_id'],samples=count,
            max_joint_step_deg=.1,failures=[],passed=True)]

    def test_reject_then_select_preserves_source_and_has_no_execution_permission(self):
        output,ctx,args=self.fixture();before=state_binding(output,ctx);calls=[]
        def audit(f):
            calls.append(f);result=self.clear(f)
            if len(calls)<3:result[0].update(passed=False,failures=[dict(virtual_ns=1,fraction=1.)])
            return result
        result=recommend_segment(output,**dict(args,audit=audit))
        self.assertEqual(result['recommended_reference_speed'],40)
        self.assertEqual([a['reference_speed'] for a in result['attempts']],[60,50,40])
        self.assertEqual(before,state_binding(output,ctx))
        self.assertFalse(result['execution_authorized']);self.assertFalse(result['whole_skill_verified'])

    def test_all_rejected_does_not_choose_fastest_anyway(self):
        output,ctx,args=self.fixture()
        def audit(f):
            value=self.clear(f);value[0].update(passed=False,failures=[dict(virtual_ns=1,fraction=1.)]);return value
        result=recommend_segment(output,**dict(args,audit=audit))
        self.assertIsNone(result['recommended_reference_speed']);self.assertEqual(len(result['attempts']),3)

    def test_context_change_during_compute_discards_clear_result(self):
        for field in ('epoch','calibration_id','control_profile_sha256','observation','source_plan_sha256','request_id'):
            with self.subTest(field=field):
                output,ctx,args=self.fixture()
                def audit(f):
                    ctx[field]='changed';return self.clear(f)
                with self.assertRaisesRegex(RecommendationInvalidated,'context_or_output_changed'):
                    recommend_segment(output,**dict(args,audit=audit))

    def test_same_joint_position_but_changed_internal_state_or_limits_invalidates(self):
        for field in ('velocity','max_jerk','target_cutoff_hz','bounds','timeout'):
            with self.subTest(field=field):
                output,ctx,args=self.fixture();position=output.position.copy()
                def audit(f):
                    if field=='velocity':output.planner._radians.velocity[7]+=.01
                    elif field=='bounds':output.planner.bounds[7,1]-=.01
                    elif field=='timeout':output.timeout+=.01
                    else:setattr(output.planner._radians,field,getattr(output.planner._radians,field)+.1)
                    return self.clear(f)
                with self.assertRaisesRegex(RecommendationInvalidated,'context_or_output_changed'):
                    recommend_segment(output,**dict(args,audit=audit))
                np.testing.assert_array_equal(position,output.position)

    def test_cancel_and_deadline_after_audit_never_return_recommendation(self):
        for action in ('cancel','deadline','fault'):
            with self.subTest(action=action):
                output,ctx,args=self.fixture();cancel=[False];now=[0.]
                def audit(f):
                    if action=='cancel':cancel[0]=True
                    elif action=='deadline':now[0]=31.
                    else:output.fault='injected'
                    return self.clear(f)
                with self.assertRaises(RecommendationInvalidated):
                    recommend_segment(output,**dict(args,audit=audit,cancelled=lambda:cancel[0],clock=lambda:now[0]))

    def test_foreign_empty_truncated_or_inconsistent_audit_is_not_a_pass(self):
        for error in ('empty','count','request','skill','passed','density'):
            with self.subTest(error=error):
                output,ctx,args=self.fixture()
                def audit(f):
                    value=self.clear(f)
                    if error=='empty':return []
                    key,bad={'count':('samples',0),'request':('request_id','old'),
                        'skill':('skill','grasp'),'passed':('passed',False),
                        'density':('max_joint_step_deg',1.)}[error]
                    value[0][key]=bad;return value
                with self.assertRaisesRegex(RecommendationInvalidated,'audit_incomplete'):
                    recommend_segment(output,**dict(args,audit=audit))

    def test_unregistered_candidates_and_unbound_stage_schedule_are_refused(self):
        output,ctx,args=self.fixture()
        for speeds in ([40,40],[70],[],[True]):
            with self.assertRaisesRegex(ValueError,'reference_candidate'):
                recommend_segment(output,**dict(args,reference_speeds=speeds))
        with self.assertRaisesRegex(ValueError,'stage_schedule_mismatch'):
            recommend_segment(output,**dict(args,geometry_trace=[dict(start_index=0,end_index=2)]))

    def test_native_error_is_not_recast_as_collision_or_clear(self):
        output,ctx,args=self.fixture()
        def audit(f):raise RuntimeError('native_service_failure')
        with self.assertRaisesRegex(RuntimeError,'native_service_failure'):
            recommend_segment(output,**dict(args,audit=audit))

    def test_explicit_rejected_prefix_can_fallback_but_partial_clear_cannot(self):
        output,ctx,args=self.fixture();calls=[]
        def audit(f):
            value=self.clear(f);calls.append(f)
            if len(calls)==1:
                value[0].update(samples=1,complete=False,stopped_on_first_failure=True,
                    passed=False,failures=[dict(virtual_ns=f['rows'][0]['virtual_ns'],fraction=1.)])
            return value
        result=recommend_segment(output,**dict(args,audit=audit))
        self.assertEqual(result['recommended_reference_speed'],50)
        for wrong in (dict(samples=1),dict(complete=False),dict(samples=1,stopped_on_first_failure=True)):
            with self.subTest(wrong=wrong):
                def incomplete(f):
                    value=self.clear(f);value[0].update(wrong);return value
                with self.assertRaisesRegex(RecommendationInvalidated,'audit_incomplete'):
                    recommend_segment(output,**dict(args,audit=incomplete))

    def test_learned_preference_is_freshly_checked_and_can_fail_back(self):
        output,ctx,args=self.fixture()
        ctx['planning_scope']=dict(task='same-task',recipe='same-recipe')
        args['context']=deepcopy(ctx)
        def clear(f):
            value=self.clear(f);value[0]['complete']=True;return value
        discovery=recommend_segment(output,**dict(args,audit=clear,reference_speeds=[40,60,50]))
        hint=propose_preference(discovery,'previous-independent-run')
        calls=[]
        def changed_scene_audit(f):
            calls.append(f);value=clear(f)
            if len(calls)==1:value[0].update(passed=False,failures=[dict(virtual_ns=1,fraction=1.)])
            return value
        ctx['observation']['pose'][0]=.01;args['context']=deepcopy(ctx)
        result=recommend_segment(output,**dict(args,audit=changed_scene_audit,preferences=[hint]))
        self.assertTrue(result['preference_usage']['applied'])
        self.assertEqual(result['evaluated_order'],[40,60,50])
        self.assertEqual([a['reference_speed'] for a in result['attempts']],[40,60])
        self.assertEqual(result['recommended_reference_speed'],60)
        self.assertEqual(len(calls),2)

    def test_cancellation_still_discards_preferred_candidate(self):
        output,ctx,args=self.fixture();ctx['planning_scope']=dict(task='same',recipe='same');args['context']=deepcopy(ctx)
        def clear(f):
            value=self.clear(f);value[0]['complete']=True;return value
        hint=propose_preference(recommend_segment(output,**dict(args,audit=clear)),'prior')
        cancelled=[False]
        def audit(f):
            cancelled[0]=True;return clear(f)
        with self.assertRaisesRegex(RecommendationInvalidated,'cancelled'):
            recommend_segment(output,**dict(args,audit=audit,preferences=[hint],cancelled=lambda:cancelled[0]))


if __name__=='__main__':unittest.main()

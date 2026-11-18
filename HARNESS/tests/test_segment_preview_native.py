"""Forward-only planned reference forecasts; native unittest, no hardware."""
import importlib.util
from pathlib import Path
import sys
import unittest
import numpy as np

ROOT=Path(__file__).parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from segment_preview import preview_segment
from output_branch import fork_output
from f1_harness.trajectory import schedule


@unittest.skipUnless(importlib.util.find_spec('ruckig') is not None,'requires native numerical environment')
class SegmentPreviewTests(unittest.TestCase):
    def fixture(self):
        from f1dagger.robot_io import make_output
        output=make_output({'_infra_root':str(ROOT.parent/'F1-4242')})
        output.planner.set_limits([[-170.,170.]]*14)
        q=np.zeros(14);output.push(q,1.,enabled=(False,True))
        target=q.copy();target[7]=20.
        for i in range(1,15):
            now=1.+i*.02;output.push(target,now,enabled=(False,True));output.sample(now)
        measured=np.r_[output.position,0.,100.]
        targets=[measured.copy(),measured.copy()]
        targets[0][7]+=4.;targets[1][7]+=8.
        trajectory=schedule(targets,measured,[dict(start_index=0,end_index=1),dict(start_index=1,end_index=2)],20.)
        kwargs=dict(skill='transfer_above_hole',request_id='forecast',trajectory=trajectory,
            targets=targets,start_index=0,measured16=measured,first_publication_ns=1_300_000_000)
        return output,kwargs

    def test_moving_state_is_preserved_and_no_future_boundary_is_consumed(self):
        output,kwargs=self.fixture();_,before=fork_output(output)
        result=preview_segment(output,**kwargs)
        _,after=fork_output(output);self.assertEqual(before,after)
        self.assertEqual(result['input_state'],before)
        self.assertGreater(max(abs(x) for x in before['native_state']['velocity']),.1)
        self.assertEqual(result['end_index'],0)
        self.assertTrue(result['rows'][-1]['at_reference_boundary'])
        self.assertTrue(all(r['reference_index']==0 for r in result['rows']))
        self.assertFalse(result['measured_arrival_predicted'])
        self.assertFalse(result['execution_authorized'])

    def test_first_two_references_use_anchor_then_clock_advances(self):
        output,kwargs=self.fixture();rows=preview_segment(output,**kwargs)['rows']
        np.testing.assert_array_equal(rows[0]['reference16'],kwargs['measured16'])
        np.testing.assert_array_equal(rows[1]['reference16'],kwargs['measured16'])
        self.assertGreater(rows[2]['reference16'][7],rows[1]['reference16'][7])
        self.assertEqual(rows[1]['virtual_ns']-rows[0]['virtual_ns'],20_000_000)

    def test_reject_contact_gripper_and_inactive_changes(self):
        output,kwargs=self.fixture()
        with self.assertRaisesRegex(ValueError,'free_space_only'):
            preview_segment(output,**dict(kwargs,skill='grasp'))
        for field in (0,14,15):
            with self.subTest(field=field):
                changed=[q.copy() for q in kwargs['targets']];changed[0][field]+=.1
                trajectory=schedule(changed,kwargs['measured16'],[dict(start_index=0,end_index=1),dict(start_index=1,end_index=2)],20.)
                with self.assertRaisesRegex(ValueError,'inactive_arm_or_gripper'):
                    preview_segment(output,**dict(kwargs,targets=changed,trajectory=trajectory))

    def test_clock_and_start_state_must_match_current_committed_output(self):
        output,kwargs=self.fixture()
        for stamp in (1_280_000_000,1_400_000_000):
            with self.subTest(stamp=stamp),self.assertRaisesRegex(ValueError,'current_sample'):
                preview_segment(output,**dict(kwargs,first_publication_ns=stamp))
        bad=kwargs['measured16'].copy();bad[8]+=.001
        with self.assertRaisesRegex(ValueError,'match_current_output'):
            preview_segment(output,**dict(kwargs,measured16=bad))
        with self.assertRaisesRegex(ValueError,'50hz_clock'):
            preview_segment(output,**dict(kwargs,tick_ns=10_000_000))

    def test_non_boundary_start_or_invalid_schedule_is_refused(self):
        output,kwargs=self.fixture()
        trajectory=dict(kwargs['trajectory'],stops=[1])
        with self.assertRaisesRegex(ValueError,'registered_segment_start'):
            preview_segment(output,**dict(kwargs,trajectory=trajectory,start_index=1))
        with self.assertRaisesRegex(ValueError,'invalid_preview_schedule'):
            preview_segment(output,**dict(kwargs,trajectory=dict(trajectory,times_s=[0.,.1])))

    def test_sample_budget_does_not_mutate_source(self):
        output,kwargs=self.fixture();_,before=fork_output(output)
        with self.assertRaisesRegex(RuntimeError,'sample_budget'):
            preview_segment(output,**dict(kwargs,max_samples=1))
        self.assertEqual(before,fork_output(output)[1])

    def test_wall_budget_does_not_mutate_source(self):
        output,kwargs=self.fixture();_,before=fork_output(output)
        ticks=iter([0.,1.,2.])
        with self.assertRaisesRegex(RuntimeError,'wall_budget'):
            preview_segment(output,**dict(kwargs,max_wall_s=.1,clock=lambda:next(ticks)))
        self.assertEqual(before,fork_output(output)[1])

    def test_second_segment_uses_registered_origin_without_future_samples(self):
        output,kwargs=self.fixture()
        result=preview_segment(output,**dict(kwargs,start_index=1))
        self.assertEqual(result['start_index'],1)
        np.testing.assert_array_equal(result['rows'][0]['reference16'],kwargs['targets'][0])
        self.assertEqual(result['end_index'],1)

    def test_conditional_hold_extends_same_boundary_without_new_segment(self):
        output,kwargs=self.fixture();_,before=fork_output(output)
        prefix=preview_segment(output,**kwargs)
        extended=preview_segment(output,**kwargs,post_boundary_samples=40)
        self.assertEqual(extended['rows'][:len(prefix['rows'])],prefix['rows'])
        self.assertEqual(len(extended['rows']),len(prefix['rows'])+40)
        self.assertEqual(extended['reference_boundary_ns'],prefix['rows'][-1]['virtual_ns'])
        for row in extended['rows'][len(prefix['rows']):]:
            self.assertEqual(row['reference16'],kwargs['targets'][0].tolist())
            self.assertEqual(row['reference_index'],0)
            self.assertTrue(row['at_reference_boundary'])
        self.assertNotEqual(extended['rows'][-1]['submitted16'],prefix['rows'][-1]['submitted16'])
        self.assertFalse(extended['boundary_completion_predicted'])
        self.assertFalse(extended['measured_arrival_predicted'])
        self.assertTrue(extended['conditional_hold']['requires_no_new_segment'])
        self.assertEqual(before,fork_output(output)[1])

    def test_tail_budget_refuses_partial_result_and_preserves_source(self):
        output,kwargs=self.fixture();_,before=fork_output(output)
        prefix=preview_segment(output,**kwargs)
        with self.assertRaisesRegex(RuntimeError,'sample_budget'):
            preview_segment(output,**kwargs,post_boundary_samples=40,max_samples=len(prefix['rows'])+39)
        self.assertEqual(before,fork_output(output)[1])
        for count in (-1,251,True,2.5):
            with self.subTest(count=count),self.assertRaisesRegex(ValueError,'post_boundary'):
                preview_segment(output,**kwargs,post_boundary_samples=count)

    def test_later_plan_targets_cannot_change_conditional_hold(self):
        output,kwargs=self.fixture()
        first=preview_segment(output,**kwargs,post_boundary_samples=40)
        changed=[q.copy() for q in kwargs['targets']];changed[1][7]+=10.
        trajectory=schedule(changed,kwargs['measured16'],[dict(start_index=0,end_index=1),dict(start_index=1,end_index=2)],20.)
        second=preview_segment(output,**dict(kwargs,targets=changed,trajectory=trajectory),post_boundary_samples=40)
        self.assertEqual(first['rows'],second['rows'])


if __name__=='__main__':unittest.main()

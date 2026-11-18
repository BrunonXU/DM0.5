"""Separate real feedback semantics from native command-path prediction."""
from copy import deepcopy
import importlib.util
import unittest

import numpy as np

import test_segment_preview_native as fixtures
from segment_preview import preview_segment
from output_branch import fork_output
from sweep_blocks import make_blocks
from f1_harness.command_forecast import sweep_anchor


@unittest.skipUnless(importlib.util.find_spec('ruckig') is not None, 'requires native numerical environment')
class CommandForecastTests(unittest.TestCase):
    def fixture(self):
        output, legacy = fixtures.SegmentPreviewTests().fixture()
        command = legacy['measured16'].tolist()
        plan = {k: deepcopy(v) for k, v in legacy.items() if k != 'measured16'}
        plan.update(publication=dict(sample_ns=1_280_000_000, sample_clock='test_virtual',
            record=dict(publication_complete=True, stage='completed', command_sequence=17,
                attempted_command=command, completed_monotonic_ns=8_000_000_000,
                action_source_timestamps_ns=[7_999_000_000]*14+[0,7_000_000_000])),
            feedback=dict(measured16=command.copy(), source_stamps_ns=[1_280_000_000]*16,
                received_ns=1_300_000_000, sample_clock='test_virtual'))
        return output, legacy, plan

    def test_tracking_error_is_preserved_without_reset_or_inferred_arrival(self):
        output, legacy, plan = self.fixture()
        before = fork_output(output)[1]
        old = preview_segment(output, **legacy)
        # Sensor offset and gripper lag must not masquerade as a command reset.
        plan['feedback']['measured16'][12] -= .2345
        plan['feedback']['measured16'][15] -= 7.
        sensor = deepcopy(plan['feedback'])
        new = preview_segment(output, **plan)
        self.assertEqual(new['feedback'], sensor)
        self.assertEqual(before, fork_output(output)[1])
        self.assertEqual(before, new['input_state'])
        self.assertGreater(max(abs(v) for v in before['native_state']['velocity']), .1)
        self.assertEqual([r['submitted16'] for r in old['rows']], [r['submitted16'] for r in new['rows']])
        self.assertTrue(all('measured16' not in r for r in new['rows']))
        self.assertFalse(new['physical_feedback_predicted'])
        self.assertFalse(new['measured_arrival_predicted'])
        self.assertFalse(new['execution_authorized'])
        self.assertTrue(new['requires_current_physical_revalidation'])
        self.assertAlmostEqual(new['initial_command_minus_feedback16'][12], .2345)
        self.assertEqual(new['initial_command_minus_feedback16'][15], 7.)
        # A command sweep starts at the submitted state, never the offset sensor.
        jobs = make_blocks(new['rows'], [0.]*22, None)
        self.assertEqual(jobs[0][2], legacy['measured16'].tolist())
        # Neither callers nor a consumer can mutate original receipts through output.
        new['feedback']['measured16'][0] = 99.
        self.assertEqual(plan['feedback'], sensor)

    def test_partial_publication_wrong_sequence_or_state_is_not_prediction(self):
        for field, value in [('publication_complete',False),('stage','publish'),
                             ('command_sequence',True),('command_sequence',0)]:
            output, _, plan = self.fixture()
            plan['publication']['record'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError,'complete_publication'):
                preview_segment(output, **plan)
        output, _, plan = self.fixture()
        plan['publication']['record']['attempted_command'][8] += .001
        with self.assertRaisesRegex(ValueError,'match_current_output'):
            preview_segment(output, **plan)

    def test_distinct_receipt_clock_and_old_gripper_stamps_are_retained(self):
        output, _, plan = self.fixture()
        result = preview_segment(output, **plan)
        self.assertEqual(result['publication'],plan['publication'])
        self.assertEqual(result['feedback'],plan['feedback'])
        plan['publication']['sample_ns'] += 1
        with self.assertRaisesRegex(ValueError,'publication_sample_mismatch'):
            preview_segment(output, **plan)

    def test_clock_mismatch_invalid_feedback_and_ambiguous_inputs(self):
        output, legacy, plan = self.fixture()
        with self.assertRaisesRegex(ValueError,'ambiguous_legacy'):
            preview_segment(output, **plan, measured16=legacy['measured16'])
        bad = deepcopy(plan);bad['feedback']['sample_clock']='different_owner'
        with self.assertRaisesRegex(ValueError,'sampling_clock_mismatch'):
            preview_segment(output, **bad)
        for field, value, error in [('measured16',[float('nan')]*16,'finite_vector'),
                ('source_stamps_ns',[1]*15,'timestamps_required'),
                ('source_stamps_ns',[2_000_000_000]*16,'after_receipt')]:
            bad=deepcopy(plan);bad['feedback'][field]=value
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,error):
                preview_segment(output, **bad)

    def test_prediction_cannot_be_mixed_with_feedback_or_discontinuous_commands(self):
        output, _, plan = self.fixture()
        rows=preview_segment(output, **plan)['rows']
        for kind, error in [('sensor','claim_measurement'),('mixed','mixed_sweep'),
                            ('jump','command_discontinuous')]:
            bad=deepcopy(rows)
            if kind=='sensor':bad[1]['measured16']=[0.]*16
            if kind=='mixed':bad[1].pop('evidence_kind')
            if kind=='jump':bad[1]['previous_submitted16'][8]+=.01
            with self.subTest(kind=kind),self.assertRaisesRegex(ValueError,error):
                sweep_anchor(bad)


if __name__=='__main__':unittest.main()

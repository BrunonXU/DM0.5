"""Check causal sampling, control gaps, and episode-tail supervision semantics."""
import sys
from pathlib import Path
import unittest
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from build_cleaning_dataset import previous_sample, chunk_starts, gripper_pulses


class CleaningAlignmentTests(unittest.TestCase):
    def test_never_uses_future_feedback_or_hides_long_gap(self):
        times=np.asarray([1_000_000_000,1_033_000_000,3_633_000_000])
        grid=np.asarray([990_000_000,1_020_000_000,2_000_000_000,3_634_000_000])
        values,valid,indices,_=previous_sample(times,np.asarray([10.,20.,30.]),grid,100_000_000)
        self.assertEqual(indices.tolist(),[-1,0,1,2])
        self.assertEqual(valid[:,0].tolist(),[False,True,False,True])
        self.assertTrue(np.isnan(values[0,0]) and np.isnan(values[2,0]))
        self.assertEqual(values[1,0],10.)

    def test_chunks_do_not_cross_gap_or_repeat_end_action(self):
        valid=np.asarray([True,True,True,False,True,True,True,True])
        self.assertEqual(chunk_starts(valid,3).tolist(),[True,False,False,False,True,True,False,False])
        self.assertFalse(chunk_starts(np.ones(2,dtype=bool),3).any())

    def test_gripper_review_catches_partial_opening_without_claiming_cycles(self):
        times=np.asarray([0,1,2,16,17,18,67,68,69])*10**9
        values=np.asarray([100,50,100,0,0,100,30,40,100])
        events=gripper_pulses(times,values,0)
        self.assertEqual([e['start_s'] for e in events],[1,16,67])
        self.assertTrue(all('not_verified_release' in e['meaning'] for e in events))


if __name__=='__main__':unittest.main()

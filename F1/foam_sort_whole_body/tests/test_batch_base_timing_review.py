import importlib.util
from pathlib import Path
import unittest
import numpy as np

path=Path(__file__).resolve().parents[1]/'scripts/batch_base_timing_review.py'
spec=importlib.util.spec_from_file_location('batch_base',path)
batch=importlib.util.module_from_spec(spec);spec.loader.exec_module(batch)


def roi(moving=False):
    return dict(valid=True,zero_shift_ncc=.99 if not moving else .1,shifted_ncc=.99,
                displacement_px=0.01 if not moving else 3)


def pair():
    inner=dict(t0=0.,t1=.5,pose_ok=True,direction='unknown',rois=[roi() for _ in range(3)])
    outer=dict(inner);outer['rois']=[roi() for _ in range(5)]
    central=dict(start_s=0.,end_s=.5,previous_causal_age_s=.02,current_causal_age_s=.02,
                 repeated_source_frame=False,repeated_jpeg=False,repeated_gray=False,
                 motion=dict(trusted_motion=False,dx_px=None,dy_px=None,rois=[roi() for _ in range(6)]))
    return inner,outer,central


class Tests(unittest.TestCase):
    def test_unknown_is_not_zero(self):
        n=100;t=np.arange(n)/30
        p=dict(action_candidate_valid=np.ones((n,19),bool),state_valid=np.ones((n,21),bool),
               observation_candidate_valid=np.ones(n,bool),chunk_start_candidate_valid=np.ones(n,bool))
        v,s,j,c,*_=batch.proposal(t,p,[],{'phases':[]},4,[])
        self.assertTrue(np.isnan(v).all());self.assertFalse(s.any());self.assertFalse(c.any())

    def test_no_crossing_unknown_or_parent_exclusion(self):
        n=160;p=dict(observation_candidate_valid=np.ones(n,bool),chunk_start_candidate_valid=np.ones(n,bool))
        p['chunk_start_candidate_valid'][100]=False
        good=np.ones(n,bool);good[60]=False
        result=batch.chunk_mask(p,good)
        self.assertFalse(result[11:61].any());self.assertFalse(result[100]);self.assertTrue(result[0]);self.assertTrue(result[61])

    def test_static_requires_both_regions_and_no_moving_patch(self):
        a,b,c=pair();self.assertEqual(batch.classify_pair(a,b,c)['status'],'stationary_support')
        b['rois'][0]=roi(True)
        self.assertEqual(batch.classify_pair(a,b,c)['status'],'unknown')

    def test_repeated_or_pose_missing_not_static(self):
        a,b,c=pair();c['repeated_jpeg']=True
        self.assertEqual(batch.classify_pair(a,b,c)['status'],'unknown')
        c['repeated_jpeg']=False;a['pose_ok']=False
        self.assertEqual(batch.classify_pair(a,b,c)['status'],'unknown')

    def test_opposite_regions_not_majority_filled(self):
        a,b,c=pair();a['direction']='image_left';b['direction']='image_right'
        self.assertEqual(batch.classify_pair(a,b,c)['status'],'unknown')

    def test_event_does_not_cross_invalid_signal(self):
        t=np.arange(6)/30;a=np.zeros((6,19));a[3:,14]=100;v=np.ones((6,19),bool)
        self.assertEqual(len(batch.gripper_events(t,a,v)),1)
        v[2]=False
        self.assertEqual(batch.gripper_events(t,a,v),[])

    def test_do_not_merge_across_unknown_gap(self):
        groups=batch.merge_pairs([dict(start_s=0,end_s=.5,status='stationary_support'),
          dict(start_s=.5,end_s=1,status='unknown'),dict(start_s=1,end_s=1.5,status='stationary_support')])
        self.assertEqual(len(groups),2)

    def test_slow_gripper_close_is_an_event(self):
        t=np.arange(100)/30;a=np.zeros((100,19));a[:,14]=np.linspace(0,100,100)
        events=batch.gripper_events(t,a,np.ones_like(a,dtype=bool))
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['kind'],'close_command')
        self.assertAlmostEqual(events[0]['command_change'],100)

    def test_reversal_is_not_merged_as_one_event(self):
        t=np.arange(7)/30;a=np.zeros((7,19));a[:,14]=[0,3,6,0,3,6,9]
        events=batch.gripper_events(t,a,np.ones_like(a,dtype=bool))
        self.assertEqual(len(events),3)

    def test_entire_joint_row_excluded_not_arm_only(self):
        n=100;t=np.arange(n)/30
        p=dict(action_candidate_valid=np.ones((n,19),bool),state_valid=np.ones((n,21),bool),
               observation_candidate_valid=np.ones(n,bool),chunk_start_candidate_valid=np.ones(n,bool))
        pairs=[dict(start_s=0,end_s=3.3,status='stationary_support')]
        p['action_candidate_valid'][40,3]=False
        v,s,j,chunks,*_=batch.proposal(t,p,pairs,{'phases':[]},2.8,[])
        self.assertFalse(j[40]);self.assertFalse(j[t>=2.8].any());self.assertFalse(chunks[:41].any())
        self.assertTrue(np.isnan(v[t>=2.8]).all())

if __name__=='__main__':unittest.main()

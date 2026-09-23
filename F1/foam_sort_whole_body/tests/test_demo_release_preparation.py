import sys
import unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from prepare_demo_training_release import scale_base, event_gate

class ReleasePreparationTests(unittest.TestCase):
    def test_amplitude_limit_preserves_clock_arms_and_unknown(self):
        t=np.arange(151)/30;action=np.zeros((len(t),19));action[:,:16]=np.arange(16)
        take=(t>=1)&(t<=2);u=t[take]-1;action[take,16]=-.5*6*u*(1-u)
        action[-3:,16:]=np.nan
        data={'action':action,'time_ns':(t*1e9).astype(np.int64),'mask':np.ones(len(t),bool)}
        stages=[dict(role='back_out',start_s=1.,end_s=2.,axis=0,distance_prior_m=-.5)]
        out,r=scale_base(data,stages,t,.13,.65)
        np.testing.assert_array_equal(out['action'][:,:16],action[:,:16])
        np.testing.assert_array_equal(out['time_ns'],data['time_ns'])
        self.assertTrue(np.isnan(out['action'][-3:,16:]).all())
        self.assertLessEqual(r['max_discrete_acceleration'],.13001)
        self.assertTrue(np.all(out['action'][take,16]<=0))
        self.assertLess(abs(r['stages'][0]['new_prior_distance_m']),.5)

    def inputs(self):
        t=np.arange(450)/30;action=np.zeros((len(t),19));action[30:300,14]=100
        starts=np.zeros(len(t),bool);starts[:400]=True
        stages=[dict(role=r,start_s=a,end_s=b) for r,a,b in [('back_out',2,3),('right_out',4,6),('forward_out',7,9),('back_return',11,12),('left_return',13,14)]]
        return dict(action=action,chunk_start_candidate_valid=starts),stages,t

    def test_release_removed_by_windows_fails_gate(self):
        d,s,t=self.inputs();self.assertTrue(event_gate(d,s,t)['passed'])
        d['chunk_start_candidate_valid'][251:301]=False
        self.assertFalse(event_gate(d,s,t)['passed'])

    def test_gripper_event_during_base_motion_fails_gate(self):
        d,s,t=self.inputs();d['action'][300,16]=.01
        self.assertFalse(event_gate(d,s,t)['passed'])

if __name__=='__main__':unittest.main()

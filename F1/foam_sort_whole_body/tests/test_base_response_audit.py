from copy import deepcopy
from pathlib import Path
import sys,unittest
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from audit_replay_base_response import audit,integral

def fixture(gain=.2):
    n=120; origin=10**18; clock=10**12; times=np.arange(n)/20
    phase={'episode':'batch/episode_1','source_origin_ns':str(origin),'segments':[
        {'id':'out','kind':'motion','start_s':1.,'end_s':2.,'dx_m':.5,'dy_m':0.,'dyaw_rad':0.},
        {'id':'back','kind':'motion','start_s':3.,'end_s':4.,'dx_m':-.5,'dy_m':0.,'dyaw_rad':0.}]}
    start={'event':'run_start','profile':{'feedback_max_age_s':.2},'source_manifest':{'source':{'episode':phase['episode']},'duration_ns':6*10**9,'frames':{'count':n}}}
    es=[start]
    for i,t in enumerate(times):
        velocity=3*(t-1)*(2-t) if 1<=t<2 else -3*(t-3)*(4-t) if 3<=t<4 else 0.
        es.extend([{'event':'feedback','snapshot':{'base':{'received_ns':clock+round(t*1e9),'vx':gain*velocity,'vy':0.,'wz':0.}}},
            {'event':'command','index':i,'source_time_ns':origin+round(t*1e9),'scheduled_monotonic_ns':clock+round(t*1e9),'dispatch_monotonic_ns':clock+round(t*1e9),'command_action':[0.]*16+[velocity,0.,0.]}])
    es.append({'event':'source_timeline_complete'})
    return es,phase

class ResponseAuditTests(unittest.TestCase):
    def test_net_zero_does_not_hide_two_short_legs(self):
        es,p=fixture();r=audit(es,p,.01)
        self.assertEqual(len(r['segments']),2)
        self.assertTrue(all(x['estimated_shortfall_m']>.39 for x in r['segments']))
        self.assertAlmostEqual(sum(x['feedback_including_pause_m'] for x in r['segments']),0,places=6)
        self.assertFalse(r['distance_calibration_verified'])
    def test_ideal_feedback_not_automatically_physical_acceptance(self):
        es,p=fixture(1);r=audit(es,p,.01)
        self.assertTrue(all(abs(x['feedback_to_prior_ratio']-1)<.03 for x in r['segments']))
        self.assertFalse(r['training_approved'])
    def test_missing_or_mismatched_sources_rejected(self):
        es,p=fixture();bad=deepcopy(p);bad['episode']='other'
        with self.assertRaisesRegex(ValueError,'mismatch'):audit(es,bad,.01)
        with self.assertRaisesRegex(ValueError,'incomplete'):audit(es[:-1],p,.01)
    def test_gap_and_extrapolation_rejected(self):
        with self.assertRaisesRegex(ValueError,'gap'):integral([0,.1,.9,1],np.ones((4,3)),0,1,.2)
        with self.assertRaisesRegex(ValueError,'uncovered'):integral([0,1],np.ones((2,3)),-1,1,2)
    def test_actual_times_integrate_nonuniform_samples(self):
        t=np.array([0,.07,.13,.3]);v=np.column_stack([2*t,np.zeros((4,2))])
        self.assertAlmostEqual(integral(t,v,.02,.25,.2)[0],.25**2-.02**2,places=10)
    def test_same_timestamp_different_values_rejected(self):
        es,p=fixture();fs=[e for e in es if e['event']=='feedback'];fs[21]['snapshot']['base']['received_ns']=fs[20]['snapshot']['base']['received_ns'];fs[21]['snapshot']['base']['vx']+=.1
        with self.assertRaisesRegex(ValueError,'different values'):audit(es,p,.01)

if __name__=='__main__':unittest.main()

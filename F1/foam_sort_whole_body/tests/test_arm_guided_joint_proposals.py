import sys
from pathlib import Path
import unittest
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import build_arm_guided_joint_proposals as b


def inputs():
    t=np.arange(180)/30;p=dict(action_candidate_valid=np.ones((180,19),bool),state_valid=np.ones((180,21),bool),
         observation_candidate_valid=np.ones(180,bool),chunk_start_candidate_valid=np.ones(180,bool),time_ns=np.arange(180,dtype=np.int64))
    bounds=[];pairs=[];previous=0
    for i,role in enumerate(b.ROUTE):
        a=i+.3;end=i+.9
        pairs.append(dict(start_s=previous,end_s=a,status='stationary_support'))
        for key,value in [('start_s',a),('end_s',end)]:
            bounds.append(dict(role=role,boundary=key,primary=dict(status='bounded_visual_proposal',estimate_s=value,envelope_s=[value-.05,value+.05]),stable_under_pixel_thresholds=True))
        previous=end
    pairs.append(dict(start_s=previous,end_s=6.,status='stationary_support'))
    return dict(boundaries=bounds),pairs,p,t

class Tests(unittest.TestCase):
    def test_recovers_transition_windows_without_unmasking_tail(self):
        c,ps,p,t=inputs();data,r=b.build(c,ps,p,t,5.5)
        self.assertGreater(r['stationary_motion_transition_h50'],0)
        self.assertTrue(np.isnan(data['base_velocity_proposal'][t>=5.5]).all())
        for i in np.flatnonzero(data['proposed_chunk_start']):self.assertTrue(data['proposed_joint_valid'][i:i+50].all())
        self.assertTrue(p['action_candidate_valid'].all())

    def test_static_contradiction_does_not_get_overwritten(self):
        c,ps,p,t=inputs()
        c['boundaries'][1]['primary']['estimate_s']=2.
        # This also overlaps next stage: either conflict must defer, not export.
        data,r=b.build(c,ps,p,t,5.5)
        self.assertIsNone(data);self.assertEqual(r['status'],'deferred')

    def test_unstable_boundary_never_materialized(self):
        c,ps,p,t=inputs();c['boundaries'][0]['stable_under_pixel_thresholds']=False
        data,r=b.build(c,ps,p,t,5.5);self.assertIsNone(data)

    def test_no_unknown_gap_filled_with_stationary(self):
        c,ps,p,t=inputs();ps=[x for x in ps if not(.9<=x['start_s']<1.3)]
        data,r=b.build(c,ps,p,t,5.5)
        self.assertTrue(np.isnan(data['base_velocity_proposal'][(t>1)&(t<1.2)]).all())

if __name__=='__main__':unittest.main()

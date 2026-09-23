import importlib.util
from pathlib import Path
import unittest
import numpy as np

path=Path(__file__).resolve().parents[1]/'scripts/extract_arm_base_anchors.py'
spec=importlib.util.spec_from_file_location('arm_anchors',path)
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)

class Tests(unittest.TestCase):
    def test_range_and_dropouts(self):
        t=np.arange(30)/30;q=np.tile(t[:,None],(1,14));valid=np.ones_like(q,dtype=bool)
        s=a.activity(t,q,valid)
        self.assertTrue(np.isnan(s[:6]).all());np.testing.assert_allclose(s[6:],.2)
        valid[10]=False;s=a.activity(t,q,valid)
        self.assertTrue(np.isnan(s[10:17]).all())

    def test_transition_confirmation_not_one_noisy_sample(self):
        t=np.arange(40)/30;s=np.zeros(40);s[15]=1
        self.assertEqual(a.transitions(t,s,.5),[])
        s[15:30]=1;events=a.transitions(t,s,.5)
        self.assertEqual([e['kind'] for e in events],['hold_to_motion','motion_to_hold'])
        self.assertGreater(events[0]['confirmation_time_s'],events[0]['time_s'])

    def test_anchor_is_search_not_time_equality(self):
        e=[dict(kind='motion_to_hold',time_s=4.4)]
        r=a.select_anchor(e,'back_out','start_s','motion_to_hold',dict(start_s=6.0))
        self.assertEqual(r['status'],'soft_arm_anchor')
        self.assertLess(r['search_interval_s'][0],4.4)
        self.assertGreater(r['search_interval_s'][1],6.)
        self.assertNotIn('base_velocity',r)

    def test_missing_event_remains_missing(self):
        r=a.select_anchor([],'back_out','start_s','motion_to_hold',dict(start_s=6.))
        self.assertEqual(r['status'],'no_arm_anchor')

if __name__=='__main__':unittest.main()

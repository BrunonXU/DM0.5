import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from repair_joint_static_gaps import eligible_holes

def pair(a,b,status):
    return dict(start_s=a,end_s=b,dt_s=b-a,status=status,pose_ok=True,fresh=True,repeated=False)

class StaticGapTests(unittest.TestCase):
    def baseline(self):
        return [pair(0,.1,'stationary_support'),pair(.1,.2,'unknown'),pair(.2,.3,'stationary_support')]

    def test_only_bounded_short_unknown(self):
        self.assertEqual(eligible_holes(self.baseline()),[(1,2)])
        self.assertEqual(eligible_holes(self.baseline()[1:]),[])

    def test_motion_or_conflict_never_bridged(self):
        for status in ['image_up','image_down','image_left','image_right','conflict']:
            rows=self.baseline();rows[1]['status']=status
            self.assertEqual(eligible_holes(rows),[])

    def test_invalid_camera_and_pose_are_barriers(self):
        for name,value in [('pose_ok',False),('fresh',False),('repeated',True)]:
            rows=self.baseline();rows[1][name]=value
            self.assertEqual(eligible_holes(rows),[])

    def test_missing_time_and_long_unknown_not_bridged(self):
        rows=self.baseline();rows[1]['start_s']+=.01
        self.assertEqual(eligible_holes(rows),[])
        rows=[pair(0,.1,'stationary_support')]+[pair(i/10,(i+1)/10,'unknown') for i in range(1,5)]+[pair(.5,.6,'stationary_support')]
        self.assertEqual(eligible_holes(rows),[])

if __name__=='__main__':unittest.main()

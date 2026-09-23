import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import refine_arm_guided_base_boundaries as d


def roi(dx=0.,dy=0.):
    return dict(valid=True,shifted_ncc=.99,zero_shift_ncc=.99 if not(dx or dy) else .1,
                displacement_px=(dx*dx+dy*dy)**.5,dx_px=dx,dy_px=dy)


def pair(dt=.1,dx=0.,dy=0.):
    return dict(pose_ok=True,fresh=True,dt_s=dt,repeated=False,
                rois={k:[roi(dx,dy) for _ in v] for k,v in d.ROIS.items()})


class Tests(unittest.TestCase):
    def test_rates_follow_frame_spacing(self):
        self.assertEqual(d.classify(pair(.1,-.5,0)),'image_left')
        self.assertEqual(d.classify(pair(.15,-.75,0)),'image_left')

    def test_gap_and_repeated_are_unknown(self):
        self.assertEqual(d.classify(pair(.8)),'unknown')
        p=pair();p['repeated']=True;self.assertEqual(d.classify(p),'unknown')

    def test_spatial_background_static_despite_one_moving_hand(self):
        p=pair();p['rois']['central'][0]=roi(3.,0)
        self.assertEqual(d.classify(p),'stationary_support')
        p['rois']['floor']=[roi(3,0) for _ in p['rois']['floor']]
        self.assertEqual(d.classify(p),'image_right')

    def test_conflicting_background_is_not_a_stop(self):
        p=pair();p['rois']['floor']=[roi(3,0) for _ in p['rois']['floor']]
        p['rois']['outer']=[roi(-3,0) for _ in p['rois']['outer']]
        self.assertEqual(d.classify(p),'conflict')

    def test_boundary_comes_from_images_not_search_edge(self):
        pairs=[dict(start_s=i/10,end_s=(i+1)/10) for i in range(10)]
        statuses=['stationary_support']*3+['image_left']*4+['stationary_support']*3
        a=d.locate(pairs,statuses,'image_left','start_s',[0,1])
        b=d.locate(pairs,statuses,'image_left','end_s',[0,1])
        self.assertEqual(a['status'],'bounded_visual_proposal');self.assertAlmostEqual(a['estimate_s'],.35)
        self.assertEqual(b['status'],'bounded_visual_proposal');self.assertAlmostEqual(b['estimate_s'],.65)

    def test_missing_opposite_state_does_not_invent_boundary(self):
        pairs=[dict(start_s=i/10,end_s=(i+1)/10) for i in range(10)]
        r=d.locate(pairs,['unknown']*3+['image_left']*7,'image_left','start_s',[0,1])
        self.assertEqual(r['status'],'unresolved')

    def test_search_edge_must_not_hide_adjacent_observed_stop(self):
        pairs=[dict(start_s=i/10,end_s=(i+1)/10) for i in range(10)]
        statuses=['stationary_support']*3+['image_left']*4+['stationary_support']*3
        old=d.locate(pairs,statuses,'image_left','start_s',[.31,.6])
        self.assertEqual(old['reason'],'no_observed_preceding_state')
        fixed=d.locate(pairs,statuses,'image_left','start_s',[.31,.6],context_s=.5)
        self.assertEqual(fixed['status'],'bounded_visual_proposal')
        self.assertAlmostEqual(fixed['estimate_s'],.35)
        self.assertNotEqual(fixed['estimate_s'],.31)

    def test_context_does_not_approve_wide_unknown_bracket(self):
        pairs=[dict(start_s=i/10,end_s=(i+1)/10) for i in range(15)]
        statuses=['stationary_support']+['unknown']*7+['image_left']*5+['stationary_support']*2
        fixed=d.locate(pairs,statuses,'image_left','start_s',[.5,1.5],context_s=.5)
        self.assertEqual(fixed['reason'],'boundary_bracket_too_wide')

    def test_later_adjustment_does_not_extend_previous_leg(self):
        pairs=[dict(start_s=i/10,end_s=(i+1)/10) for i in range(20)]
        statuses=['stationary_support']*2+['image_up']*8+['stationary_support']*4+['image_up']*2+['stationary_support']*4
        r=d.locate(pairs,statuses,'image_up','end_s',[0,2],dict(start_s=.3,end_s=1.))
        self.assertEqual(r['status'],'bounded_visual_proposal');self.assertLess(r['estimate_s'],1.1)

if __name__=='__main__':unittest.main()

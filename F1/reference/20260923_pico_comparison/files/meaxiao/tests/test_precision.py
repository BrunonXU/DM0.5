import unittest
import numpy as np
from pico_f1.precision import PrecisionPoseMapper, precision_settings, _exp, _delta
from pico_f1.trajectory import JerkLimitedTrajectory


class PrecisionTest(unittest.TestCase):
    def test_static_jitter_suppressed_in_position_and_wrist_attitude(self):
        m=PrecisionPoseMapper(); values=[]
        for i in range(1001):
            t=i*.01; s=np.sin(2*np.pi*8*t)
            p,q=m.update([.001*s,0,0],_exp(np.array([0,0,np.deg2rad(.5)*s])),t)
            values.append([.001*s,p[0],np.deg2rad(.5)*s,_delta(q,[1,0,0,0])[2]])
        rms=np.std(np.asarray(values)[200:],axis=0)
        self.assertLess(rms[1]/rms[0],.12)
        self.assertLess(rms[3]/rms[2],.15)

    def test_millimetre_mapping_has_continuous_small_increments(self):
        m=PrecisionPoseMapper(); values=[]
        for i in range(501):
            p,_=m.update([min(i*.01,2)*.0025,0,0],[1,0,0,0],i*.01)
            values.append(p[0])
        # 5 mm intentional slow hand travel -> 1 mm command; no hardware claim.
        self.assertAlmostEqual(values[-1],.001,delta=1e-9)
        d=np.diff(values)[20:180]
        self.assertTrue(np.all(d>0))
        self.assertLess(d.max(),.000006)

    def test_fast_motion_recovers_unit_incremental_gain(self):
        m=PrecisionPoseMapper(); values=[]
        for i in range(301):
            p,_=m.update([i*.0025,0,0],[1,0,0,0],i*.01)
            values.append(p[0])
        self.assertAlmostEqual(m.position_gain,1.,places=6)
        self.assertAlmostEqual(values[-1]-values[-101],.25,places=6)

    def test_changing_gain_cannot_rescale_accumulated_offset(self):
        m=PrecisionPoseMapper(); old_p=np.zeros(3); hand=0.
        for i in range(1000):
            hand += .002 if 100<i<300 else .00001
            old_filtered = m.filtered_p.copy() if i else None
            p,_=m.update([hand,0,0],[1,0,0,0],i*.01)
            if i:
                self.assertGreaterEqual(p[0],old_p[0]-1e-12)
                # A bandwidth increase can catch up filtered lag faster than
                # this frame's raw step; verify the actual no-rescaling contract.
                np.testing.assert_allclose(p-old_p,
                    m.position_gain*(m.filtered_p-old_filtered), atol=1e-15)
            self.assertLessEqual(p[0],hand+1e-12)
            old_p=p

    def test_ordinary_slow_movement_reaches_full_gain(self):
        for speed in (.04, .06):
            m=PrecisionPoseMapper()
            for i in range(401):
                m.update([i*.01*speed,0,0],_exp(np.array([0,0,i*.01*.35])),i*.01)
            self.assertAlmostEqual(m.position_gain,1.,places=6)
            self.assertAlmostEqual(m.rotation_gain,1.,places=6)

    def test_gain_thresholds_do_not_change_position_filter_bandwidth(self):
        old=PrecisionPoseMapper({'position_slow_m_s':.025,'position_fast_m_s':.15})
        new=PrecisionPoseMapper()
        for i in range(501):
            p=[.03*i*.01+.001*np.sin(2*np.pi*8*i*.01),0,0]
            old.update(p,[1,0,0,0],i*.01);new.update(p,[1,0,0,0],i*.01)
            np.testing.assert_array_equal(old.filtered_p,new.filtered_p)
        self.assertGreater(new.position_gain,old.position_gain)

    def test_quaternion_sign_and_pi_crossing_are_continuous(self):
        m=PrecisionPoseMapper(); prev=None
        for i in range(401):
            q=_exp(np.array([0,0,np.pi-.2+i*.001]))
            if i%2:q=-q
            _,out=m.update([0,0,0],q,i*.01)
            self.assertAlmostEqual(np.linalg.norm(out),1.,places=12)
            if prev is not None:self.assertLess(np.linalg.norm(_delta(out,prev)),.0011)
            prev=out

    def test_reset_drops_pending_filter_tail_and_reanchors_exactly(self):
        m=PrecisionPoseMapper()
        for i in range(101):m.update([i*.001,0,0],[1,0,0,0],i*.01)
        m.reset(); p,q=m.update([4,2,1],[0,0,0,1],2.)
        np.testing.assert_array_equal(p,[4,2,1])
        np.testing.assert_array_equal(q,[0,0,0,1])
        p2,q2=m.update([4,2,1],[0,0,0,1],2.01)
        np.testing.assert_array_equal(p2,p);np.testing.assert_array_equal(q2,q)

    def test_invalid_stale_and_teleport_inputs_rejected(self):
        for p,q,t in (([np.nan,0,0],[1,0,0,0],.01),([0,0,0],[0,0,0,0],.01),
                      ([0,0,0],[1,0,0,0],.2),([0,0,0],[1,0,0,0],-.01),
                      ([1,0,0],[1,0,0,0],.01)):
            m=PrecisionPoseMapper();m.update([0,0,0],[1,0,0,0],0.)
            with self.assertRaises(ValueError):m.update(p,q,t)

    def test_config_rejects_invalid_settings_and_legacy_is_disabled(self):
        self.assertFalse(precision_settings()['enabled'])
        for value in ({'enabled':1},{'position_min_gain':0},{'min_cutoff_hz':float('nan')},
                      {'rotation_min_gain':2},{'max_cutoff_hz':100},{'unknown':1},
                      {'position_slow_m_s':1},[]):
            with self.assertRaises(ValueError):precision_settings(value)
        m=PrecisionPoseMapper({'enabled':False})
        m.update([0,0,0],[1,0,0,0],0.)
        np.testing.assert_array_equal(m.update([.1,0,0],[1,0,0,0],.02)[0],[.1,0,0])

    def test_small_joint_ramp_no_long_stiction_with_fine_deadband(self):
        tr=JerkLimitedTrajectory(1,.02,30,75,300,5,.0002);tr.reset([0.]);p=[]
        for i in range(1000):
            tr.commit(tr.propose([i*.0004]));p.append(tr.position[0])
        d=np.diff(p)[100:]
        self.assertTrue(np.all(d>0))
        self.assertLess(d.max(),.0006)


if __name__=='__main__':unittest.main()

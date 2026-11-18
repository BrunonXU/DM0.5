"""Time-origin invariance without replacing measured elapsed scheduling time."""
import importlib.util
from pathlib import Path
import unittest
import numpy as np

ROOT=Path(__file__).parents[1]


@unittest.skipUnless(importlib.util.find_spec('ruckig') is not None,'requires numerical environment')
class TimeOriginTests(unittest.TestCase):
    def output(self):
        from f1dagger.robot_io import make_output
        output=make_output({'_infra_root':str(ROOT.parent/'F1-4242')})
        output.planner.set_limits([[-170.,170.]]*14)
        return output

    def run_path(self,origin,pattern):
        output=self.output();q=np.linspace(-31.,28.,14);now=origin
        output.push(q,now/1e9,enabled=(False,True));rows=[]
        for i in range(250):
            now+=pattern[i%len(pattern)]
            target=q.copy();target[7]+=5.*np.sin(i*.01);target[9]-=3.*np.sin(i*.013)
            output.push(target,now/1e9,enabled=(False,True));rows.append(output.sample(now/1e9))
            self.assertIsNone(output.fault)
            np.testing.assert_array_equal(rows[-1][:7],q[:7])
        return np.array(rows)

    def test_same_integer_intervals_at_different_clock_origins_are_identical(self):
        for pattern in ([20_000_000],[19_000_000,21_000_000],[630_000,4_000_000,13_500_000,1_000_000]):
            source=self.run_path(10_000_000_000,pattern)
            for origin in (10_001_000_000,30_000_001_000_000,86_400_001_000_000):
                with self.subTest(origin=origin,pattern=pattern):
                    np.testing.assert_array_equal(source,self.run_path(origin,pattern))

    def test_real_jitter_is_not_replaced_with_nominal_period(self):
        uniform=self.run_path(10_000_000_000,[20_000_000])
        jitter=self.run_path(10_000_000_000,[19_000_000,21_000_000])
        self.assertGreater(float(np.max(abs(uniform-jitter))),1e-6)

    def test_same_interval_reaches_both_native_and_certificate_calculations(self):
        output=self.output();output.push(np.zeros(14),0.,enabled=(False,True))
        native=output.planner._radians.propose;certificate=output.planner._certified_proposal
        observed=[]
        def native_call(target,dt=None):observed.append(('native',dt));return native(target,dt=dt)
        def certificate_call(proposal,dt):observed.append(('certificate',dt));return certificate(proposal,dt)
        output.planner._radians.propose=native_call
        output.planner._certified_proposal=certificate_call
        for dt in (.02000000000000135,.02099999999999724,.00063000000000002):
            observed.clear();proposal=output.planner.propose(np.r_[np.zeros(7),np.ones(7)],dt=dt)
            self.assertEqual(observed,[('native',round(dt*1e9)/1e9),('certificate',round(dt*1e9)/1e9)])
            output.planner.commit(proposal)

    def test_watchdog_and_active_fault_latch_do_not_change(self):
        for kind in ('gap','stale','backwards'):
            output=self.output();output.push(np.zeros(14),1.,enabled=(False,True))
            if kind=='gap':output.push(np.ones(14),1.051,enabled=(False,True));stamp=1.051
            elif kind=='stale':output.updated=.8;stamp=1.02
            else:stamp=.999
            held=output.sample(stamp);fault=output.fault
            self.assertIsNotNone(fault)
            output.push(np.ones(14),stamp+.02,enabled=(False,True))
            np.testing.assert_array_equal(held,output.sample(stamp+.02))
            self.assertEqual(output.fault,fault)

    def test_invalid_intervals_remain_errors(self):
        output=self.output();output.push(np.zeros(14),1.,enabled=(False,True))
        for dt in (0.,-.01,float('nan'),float('inf'),.10000000001):
            with self.subTest(dt=dt),self.assertRaises(ValueError):
                output.planner.propose(np.ones(14),dt=dt)


if __name__=='__main__':unittest.main()

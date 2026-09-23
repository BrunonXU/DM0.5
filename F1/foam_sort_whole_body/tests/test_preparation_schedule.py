"""Generated setup motion stretches under jitter without speeding/skipping."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'robot'))
from f1_replay.runtime import run_preparation_schedule,ReplayError,ReplayInterrupted
from test_replay_runtime import FakeClock,FakeBackend,sample_bundle

class PreparationScheduleTests(unittest.TestCase):
    def setUp(self):
        self.clock=FakeClock();self.backend=FakeBackend(self.clock);self.events=[]
        self.bundle=sample_bundle(tuple(round(i*1e9/30) for i in range(1800)),60_000_000_000)
    def run_plan(self,tick=lambda:None,stopped=lambda:False):
        run_preparation_schedule(self.bundle,self.backend.publish,tick,
            lambda k,v:self.events.append((k,v)),.2,now=self.clock.now,
            sleep=self.clock.sleep,max_command_hz=31.0,stopped=stopped)
    def test_repeated_50ms_stalls_stretch_without_speedup_or_skipped_waypoints(self):
        injected=set()
        def tick():
            i=len(self.backend.sent)
            self.clock.advance(.003)
            if i in (100,694,1000,1500) and i not in injected:
                injected.add(i);self.clock.advance(.05)
        self.run_plan(tick)
        self.assertEqual(len(self.backend.sent),1800)
        self.assertEqual([x[1] for x in self.backend.sent],[tuple(f.action) for f in self.bundle.frames])
        self.assertTrue(all(b[0]-a[0]>=self.bundle.frames[i+1].time_ns-self.bundle.frames[i].time_ns
                            for i,(a,b) in enumerate(zip(self.backend.sent,self.backend.sent[1:]))))
        self.assertGreater(self.backend.sent[-1][0]-self.backend.sent[0][0],self.bundle.frames[-1].time_ns)
    def test_long_stall_stops_before_next_command(self):
        def tick():
            if len(self.backend.sent)==5:self.clock.advance(.25)
        with self.assertRaisesRegex(ReplayError,'连续卡顿'):self.run_plan(tick)
        self.assertEqual(len(self.backend.sent),5)
    def test_feedback_error_still_stops(self):
        def tick():
            if len(self.backend.sent)==4:raise ReplayError('反馈过旧')
        with self.assertRaisesRegex(ReplayError,'反馈过旧'):self.run_plan(tick)
        self.assertEqual(len(self.backend.sent),4)
    def test_operator_interrupt_prevents_next_dispatch(self):
        with self.assertRaises(ReplayInterrupted):self.run_plan(stopped=lambda:len(self.backend.sent)==3)
        self.assertEqual(len(self.backend.sent),3)
    def test_slow_send_stops_without_a_followup_command(self):
        publish=self.backend.publish
        def slow(a):
            r=publish(a);self.clock.advance(.25);return r
        self.backend.publish=slow
        with self.assertRaisesRegex(ReplayError,'发送或日志卡顿'):self.run_plan()
        self.assertEqual(len(self.backend.sent),1)
if __name__=='__main__':unittest.main()

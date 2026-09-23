from pathlib import Path
from types import SimpleNamespace
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'robot'))
from f1_replay.cli import select_recording_topics
from f1_replay.runtime import ReplayError
class DiagnosticRecordingTests(unittest.TestCase):
    def test_optional_streams_never_replace_required_or_enable_control(self):
        p={'topics':{'a':'/cmd','b':'/state'},'record_topics':['/camera'],
           'diagnostic_record_topics':['/wheels','/missing','/camera','/wheels']}
        backend=SimpleNamespace(topic_peers=lambda _: {'/wheels':{'publishers':[{'name':'driver'}]},'/missing':{'subscribers':[{'name':'observer'}]}})
        topics,r=select_recording_topics(p,backend)
        self.assertEqual(topics,['/cmd','/state','/camera','/wheels'])
        self.assertEqual(r['not_discovered'],['/missing'])
    def test_empty_keeps_previous_behavior(self):
        topics,r=select_recording_topics({'topics':{'a':'/cmd'},'record_topics':['/camera']},SimpleNamespace())
        self.assertEqual(topics,['/cmd','/camera']);self.assertEqual(r['selected'],[])
    def test_malformed_config_rejected(self):
        for invalid in ['/foo',['relative'],['/space here'],[None]]:
            with self.subTest(invalid=invalid),self.assertRaises(ReplayError):select_recording_topics({'topics':{},'record_topics':[],'diagnostic_record_topics':invalid},SimpleNamespace())
if __name__=='__main__':unittest.main()

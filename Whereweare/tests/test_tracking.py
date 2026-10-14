import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hub import Hub, HubError
from review_jobs import ReviewJobs
from review_store import ReviewStore


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'projects.json'
        self.config.write_text(json.dumps({'projects':[
            {'id':'work','name':'业务','kind':'delivery','source_paths':[]},
            {'id':'tool','name':'工具','kind':'tool','source_paths':[]}]}))
        self.hub = Hub(self.root,self.root/'runtime',self.config)

    def test_manual_tier_and_archive_survive_restart_without_erasing_events(self):
        node = self.hub.add_timeline_node({'project_id':'work','title':'保留证据节点'})
        self.assertEqual(self.hub.tracking_preferences()['work']['tier'],'secondary')
        self.assertEqual(self.hub.tracking_preferences()['tool']['tier'],'support')
        self.hub.update_tracking('work',{'tier':'main','archived':True})
        reopened = Hub(self.root,self.root/'runtime',self.config)
        self.assertEqual(reopened.tracking_preferences()['work']['tier'],'main')
        self.assertTrue(reopened.tracking_preferences()['work']['archived'])
        reopened.update_tracking('work',{'archived':False})
        self.assertEqual(reopened.state()['timeline_nodes'][0],node)
        self.assertEqual(reopened.tracking_preferences()['work']['tier'],'main')

    def test_invalid_preferences_and_review_modes_leave_state_unchanged(self):
        initial=self.hub.tracking_preferences()
        for body in ({'archived':'yes'},{'tier':'urgent'},{'tier':[]},{'id':'other'},{'status':'done'},{}):
            with self.assertRaises(HubError): self.hub.update_tracking('work',body)
        with self.assertRaises(HubError):self.hub.update_tracking('absent',{'archived':True})
        self.assertEqual(self.hub.tracking_preferences(),initial)
        jobs=ReviewJobs(self.hub,ReviewStore(self.hub),binary='')
        for value in ([],{},None,'yearly'):
            with self.assertRaises(HubError):jobs.start(value)
        with self.assertRaises(HubError):jobs.start('daily')
        self.assertIsNone(jobs.state()['job'])

    def test_activity_is_manual_nullable_and_independent_of_tier_archive(self):
        self.assertIsNone(self.hub.tracking_preferences()['work']['activity'])
        self.hub.update_tracking('work', {'tier': 'main', 'activity': 'idea', 'archived': True})
        self.hub.update_tracking('work', {'activity': 'current'})
        prefs = Hub(self.root, self.root/'runtime', self.config).tracking_preferences()['work']
        self.assertEqual(prefs['activity'], 'current')
        self.assertEqual(prefs['tier'], 'main')
        self.assertTrue(prefs['archived'])
        for value in ('active', False, [], {}):
            with self.assertRaises(HubError): self.hub.update_tracking('work', {'activity': value})
        self.hub.update_tracking('work', {'activity': None})
        self.assertIsNone(self.hub.tracking_preferences()['work']['activity'])

    def test_activity_change_invalidates_review_but_unchanged_save_does_not(self):
        store = ReviewStore(self.hub)
        packet = store.prepare()
        initial = packet['tracking_fingerprint']
        self.hub.update_tracking('work', {'activity': 'current'})
        self.assertNotEqual(store.prepare()['tracking_fingerprint'], initial)
        changed = store.prepare()['tracking_fingerprint']
        self.hub.update_tracking('work', {'activity': 'current'})
        self.assertEqual(store.prepare()['tracking_fingerprint'], changed)

    def test_legacy_registry_exposes_new_defaults(self):
        project = self.hub.state()['projects'][0]
        self.assertEqual(project['platform'], 'shared')
        self.assertEqual(project['supports'], [])

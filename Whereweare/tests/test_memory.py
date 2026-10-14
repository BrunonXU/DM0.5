import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_review_store as fixtures
from hub import HubError
from memory import action_key, decisions, save_decision
from review_jobs import ReviewJobs


class MemoryTests(unittest.TestCase):
    setUp = fixtures.ReviewStoreTests.setUp
    candidate = fixtures.ReviewStoreTests.candidate
    publish = fixtures.ReviewStoreTests.publish

    def test_pause_survives_reviews_and_resume_keeps_history(self):
        first = self.publish()
        key = action_key(first['projects'][0]['next_step'])
        request = {'project_id': 'demo', 'object_key': key, 'status': 'paused'}
        decision = save_decision(self.hub, first, request)
        self.assertEqual(decision['status'], 'paused')
        packet = self.store.prepare()
        self.assertEqual(packet['user_decisions'][0]['status'], 'paused')
        self.store.publish(self.candidate(), packet['id'])
        self.assertEqual(decisions(self.hub)[0]['status'], 'paused')
        save_decision(self.hub, first, {**request, 'status': 'active'})
        self.assertEqual(decisions(self.hub)[0]['history'][0]['status'], 'paused')
        self.assertEqual(decisions(self.hub)[0]['status'], 'active')
        with self.assertRaises(HubError):
            save_decision(self.hub, first, {**request, 'object_key': 'next:unknown'})

    def test_partial_acknowledgement_does_not_mark_other_lines_read(self):
        packet = self.store.prepare()
        candidate = self.candidate()
        candidate['reviewed_sources'] = [{'path': 'demo/docs/STATUS.md', 'start_line': 1, 'end_line': 2}]
        with self.assertRaisesRegex(HubError, '尚未读取'):
            self.store.publish(candidate, packet['id'])
        candidate['projects'], candidate['events'] = [], []
        result = self.store.publish(candidate, packet['id'])
        self.assertEqual(result['coverage']['demo/docs/STATUS.md']['through_line'], 2)
        self.assertNotIn('demo/docs/STATUS.md', result['source_manifest'])
        next_packet = self.store.prepare()
        self.assertEqual(next_packet['sources'][0]['start_line'], 3)
        self.store.publish(self.candidate(), next_packet['id'])
        self.assertFalse(self.store.state()['freshness']['stale'])

    def test_decisions_invalidate_report_cache_without_changing_facts(self):
        first = self.publish()
        packet = self.store.prepare_report('daily', '2026-10-12')
        self.store.publish_report(self.candidate(), packet['id'])
        save_decision(self.hub, first, {'project_id': 'demo', 'object_key': action_key(first['projects'][0]['next_step']), 'status': 'paused'})
        self.assertIsNone(self.store.cached_report('daily', '2026-10-12'))
        self.assertEqual(self.store.state()['snapshot'], first)

    def test_explicit_source_supplement_is_hash_bound(self):
        packet = self.store.prepare()
        source = self.store.read_source(packet['id'], 'demo/docs/STATUS.md', 3, 4)
        self.assertEqual(source['start_line'], 3)
        self.assertIn('现场待验收', source['content'])
        self.source.write_text('changed')
        with self.assertRaises(HubError):
            self.store.read_source(packet['id'], 'demo/docs/STATUS.md', 1, 1)

    def test_automation_disabled_by_default_debounces_and_does_not_retry_same_change(self):
        self.hub.update_tracking('demo', {'activity': 'current'})
        self.publish()
        self.source.write_text(self.source.read_text()+'\n资料变更\n')
        self.hub.scan()
        jobs = ReviewJobs(self.hub, self.store, binary='/bin/true')
        self.assertFalse(jobs.automation()['enabled'])
        clock = [1000]
        with patch('review_jobs.time.monotonic', side_effect=lambda: clock[0]), patch.object(jobs, 'start') as start:
            jobs.tick()
            start.assert_not_called()
            jobs.configure_automation({'enabled': True, 'daily_limit': 1})
            clock[0] = 1010; jobs.tick()
            clock[0] = 1120; jobs.tick(); start.assert_not_called()
            clock[0] = 1140; jobs.tick(); start.assert_called_once_with('update', project_ids={'demo'})
            clock[0] = 1300; jobs.tick(); self.assertEqual(start.call_count, 1)
        with self.assertRaises(HubError):
            jobs.configure_automation({'daily_limit': 0})

    def test_source_scope_cannot_include_unregistered_files(self):
        with self.assertRaises(HubError):
            self.store.prepare(source_paths=['outside/private.md'])
        packet = self.store.prepare(source_paths=['demo/docs/STATUS.md'])
        self.assertEqual([s['path'] for s in packet['sources']], ['demo/docs/STATUS.md'])

    def test_partial_review_is_not_mislabelled_new_source_change(self):
        packet = self.store.prepare()
        candidate = self.candidate()
        candidate['projects'], candidate['events'] = [], []
        candidate['reviewed_sources'] = [{'path': 'demo/docs/STATUS.md', 'start_line': 1, 'end_line': 2}]
        self.store.publish(candidate, packet['id'])
        fresh = self.store.state()['freshness']
        self.assertTrue(fresh['stale'])
        self.assertEqual(fresh['content_changed_paths'], [])
        self.source.write_text(self.source.read_text()+'\nnew evidence\n')
        self.assertIn('demo/docs/STATUS.md', self.store.state()['freshness']['content_changed_paths'])

    def test_superseded_event_remains_in_history_but_leaves_report(self):
        first = self.publish()
        old_event = first['events'][0]
        packet = self.store.prepare()
        candidate = self.candidate()
        replacement = copy.deepcopy(candidate['events'][0])
        replacement['id'] = 'replacement'
        replacement['supersedes'] = [old_event['id']]
        candidate['events'] = [replacement]
        result = self.store.publish(candidate, packet['id'])
        self.assertEqual(next(e for e in result['events'] if e['id'] == old_event['id'])['lifecycle'], 'superseded')
        report = self.store.prepare_report('daily', old_event['date'])
        self.assertNotIn(old_event['id'], [e['id'] for e in report['period_events']])

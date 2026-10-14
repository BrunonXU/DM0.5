import copy
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hub import Hub, HubError
from review_store import ReviewStore, DOCUMENT_TEXT_LIMIT, TOTAL_TEXT_LIMIT


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.preferences = self.workspace / "preferences.json"
        preference_path = patch("review_store.PREFERENCES_PATH", self.preferences)
        preference_path.start()
        self.addCleanup(preference_path.stop)
        (self.workspace / "demo/docs").mkdir(parents=True)
        self.source = self.workspace / "demo/docs/STATUS.md"
        self.source.write_text("# 当前状态\n\n2026-10-12：离线读取通过；现场待验收。\n目标：完成现场验收。\n", encoding="utf-8")
        self.config = self.workspace / "projects.json"
        self.config.write_text(json.dumps({"projects": [{"id": "demo", "name": "Demo", "source_paths": ["demo/docs/STATUS.md"], "watch_paths": ["demo/docs"]},
                                                         {"id": "unknown", "name": "Unknown", "source_paths": ["unknown/STATUS.md"], "watch_paths": []}]}), encoding="utf-8")
        self.hub = Hub(self.workspace, self.workspace / "runtime", self.config)
        self.tracking = self.workspace / "tracking"
        self.store = ReviewStore(self.hub, self.tracking)

    def candidate(self):
        proof = [{"path": "demo/docs/STATUS.md", "line": 3}]
        return {"headline": "离线通过，现场待验收", "projects": [
            {"id": "demo", "headline": "离线读取通过", "goal": "完成现场验收", "next_step": "安排现场验收", "blocker": "", "evidence": copy.deepcopy(proof)}],
            "events": [{"id": "demo-read", "project_id": "demo", "date": "2026-10-12", "title": "离线读取通过",
                        "detail": "现场尚未验证", "kind": "progress", "importance": "major", "evidence": copy.deepcopy(proof)}],
            "report": {"headline": "当前基线已核对", "changes": ["离线读取通过，现场待验证"], "next": ["现场验收"], "risks": []}}

    def publish(self, mode="baseline", candidate=None):
        packet = self.store.prepare(mode)
        return self.store.publish(candidate or self.candidate(), packet["id"])

    def set_timezone(self, timezone):
        self.preferences.write_text(json.dumps({"report_timezone": timezone}), encoding="utf-8")

    def test_stages_keep_evidence_and_survive_legacy_updates(self):
        candidate = self.candidate()
        candidate['projects'][0]['stages'] = [{
            'id': 'capture', 'label': '采集', 'status': 'review', 'note': '现场待验收',
            'evidence': [{'path': 'demo/docs/STATUS.md', 'line': 3}]}]
        first = self.publish(candidate=candidate)
        stage = first['projects'][0]['stages'][0]
        self.assertTrue(stage['evidence'][0]['sha256'])
        second = self.publish(candidate=self.candidate())
        self.assertEqual(second['projects'][0]['stages'], first['projects'][0]['stages'])

    def test_stage_claims_require_project_owned_evidence(self):
        candidate = self.candidate()
        stage = {'id': 'capture', 'label': '采集', 'status': 'verified',
                 'note': '已验证', 'evidence': []}
        candidate['projects'][0]['stages'] = [stage]
        with self.assertRaisesRegex(HubError, '阶段验收状态必须有来源'):
            self.publish(candidate=candidate)
        stage['evidence'] = [{'path': 'unknown/STATUS.md', 'line': 1}]
        with self.assertRaisesRegex(HubError, '证据不属于'):
            self.publish(candidate=candidate)
        stage.update(status='unknown', note='缺验收记录', evidence=[])
        self.assertEqual(self.publish(candidate=candidate)['projects'][0]['stages'][0]['status'], 'unknown')

    def test_duplicate_stage_ids_are_rejected(self):
        candidate = self.candidate()
        stage = {'id': 'capture', 'label': '采集', 'status': 'unknown', 'note': '待确认', 'evidence': []}
        candidate['projects'][0]['stages'] = [stage, copy.deepcopy(stage)]
        with self.assertRaisesRegex(HubError, '阶段 id 重复'):
            self.publish(candidate=candidate)

    def test_workspace_repository_link_is_optional_and_not_review_evidence(self):
        self.publish()
        self.assertEqual(self.store.state()['workspace_repository_url'], '')
        self.preferences.write_text(json.dumps({
            'workspace_repository_url': 'https://github.com/BrunonXU/DM0.5'}))
        state = self.store.state()
        self.assertEqual(state['workspace_repository_url'], 'https://github.com/BrunonXU/DM0.5')
        self.assertFalse(state['freshness']['stale'])
        self.preferences.write_text(json.dumps({'workspace_repository_url': 'javascript:alert(1)'}))
        self.assertEqual(self.store.state()['workspace_repository_url'], '')

    def test_daily_has_one_next_goal_and_short_reports_are_validated(self):
        candidate = self.candidate()
        candidate['report']['next'] = ['第一个建议', '第二个建议']
        with self.assertRaises(HubError):
            self.publish('daily', candidate)
        candidate['report']['next'] = ['第一个建议']
        candidate['report']['changes'] = ['变化'] * 4
        with self.assertRaises(HubError):
            self.publish('weekly', candidate)
        candidate['report']['changes'] = ['变化']
        candidate['report']['risks'] = ['阻塞'] * 2
        with self.assertRaises(HubError):
            self.publish('weekly', candidate)

    def test_prepare_uses_declared_markdown_and_marks_baseline_and_missing(self):
        (self.workspace / "demo/docs/not_code.py").write_text("print('do not index')")
        packet = self.store.prepare("baseline")
        self.assertEqual(packet["base_revision"], 0)
        self.assertEqual(packet["changes"], [])
        self.assertEqual(packet["sources"][0]["path"], "demo/docs/STATUS.md")
        self.assertEqual(packet["sources"][0]["line_count"], 4)
        self.assertNotIn("demo/docs/not_code.py", packet["source_manifest"])
        self.assertIn({"path": "unknown/STATUS.md", "reason": "missing"}, packet["unread"])
        self.assertTrue((self.hub.runtime / "reviews" / packet["id"] / "packet.json").exists())
        self.assertFalse(self.store.state()["freshness"]["reviewed"])

    def test_custom_hub_defaults_to_isolated_tracking_and_unknown_is_allowed(self):
        isolated = ReviewStore(self.hub)
        self.assertEqual(isolated.tracking_root, self.hub.runtime / "tracking")
        candidate = self.candidate()
        candidate["projects"].append({"id": "unknown", "headline": "暂无证据", "goal": "", "goal_status": "unknown",
                                       "next_step": "补充状态文档", "blocker": "", "evidence": []})
        result = isolated.publish(candidate, isolated.prepare("baseline")["id"])
        self.assertEqual(result["projects"][1]["goal_status"], "unknown")
        self.assertTrue((self.hub.runtime / "tracking/current.json").is_file())
        self.assertFalse(self.tracking.exists())

    def test_limits_and_explicit_unread_keep_full_evidence_manifest(self):
        self.source.write_text("# 长文\n" + "一条记录\n" * 4000, encoding="utf-8")
        for number in range(12):
            (self.source.parent / f"extra-{number}.md").write_text("# 长文\n" + "更多记录\n" * 4000, encoding="utf-8")
        packet = self.store.prepare("baseline")
        self.assertLessEqual(sum(len(item["content"]) for item in packet["sources"]), TOTAL_TEXT_LIMIT)
        self.assertTrue(all(len(item["content"]) <= DOCUMENT_TEXT_LIMIT for item in packet["sources"]))
        self.assertTrue(packet["sources"][0]["truncated"])
        self.assertIn("total_text_limit", {item["reason"] for item in packet["unread"]})
        candidate = self.candidate()
        candidate["events"][0]["date"] = None
        candidate["events"][0]["evidence"][0]["line"] = 4000
        with self.assertRaisesRegex(HubError, '尚未读取'):
            self.store.publish(candidate, packet['id'])
        candidate['events'] = []
        result = self.store.publish(candidate, packet['id'])
        self.assertIn('demo/docs/STATUS.md', result['observed_manifest'])
        self.assertNotIn('demo/docs/STATUS.md', result['source_manifest'])
        followup = self.store.prepare()
        source = next(x for x in followup['sources'] if x['path'] == 'demo/docs/STATUS.md')
        self.assertGreater(source['start_line'], 1)
        self.assertIn('demo/docs/STATUS.md', followup['changed_paths'])

    def test_publish_defaults_proposed_adds_hash_and_preserves_history(self):
        first = self.publish()
        self.assertEqual(first["revision"], 1)
        self.assertEqual(first["projects"][0]["goal_status"], "proposed")
        self.assertEqual(first["report_scope"], "baseline")
        self.assertIsNone(first["period_start"])
        self.assertEqual(len(first["events"][0]["evidence"][0]["sha256"]), 64)
        self.assertFalse(self.store.state()["freshness"]["stale"])
        candidate = self.candidate()
        candidate["events"][0]["detail"] = "离线证据复核完成，仍无现场验收"
        second = self.publish("update", candidate)
        self.assertEqual(second["revision"], 2)
        self.assertEqual(len(second["events"]), 1)
        files = sorted((self.tracking / "history").glob("*.json"))
        self.assertEqual(len(files), 2)
        archived = json.loads(files[0].read_text())
        self.assertEqual(archived["events"][0]["detail"], "现场尚未验证")
        self.assertEqual(json.loads(self.store.current_path.read_text()), second)
        self.assertEqual(ReviewStore(self.hub, self.tracking).state()["snapshot"], second)

    def test_schema_and_evidence_failures_do_not_write_snapshot(self):
        packet = self.store.prepare("baseline")
        cases = []
        for transform in [
            lambda c: c.update(headline="太" * 101),
            lambda c: c.update(surprise="unsupported"),
            lambda c: c["report"].update(changes=["一"] * 6),
            lambda c: c["projects"][0].update(id="missing"),
            lambda c: c["events"][0].update(date="2026-02-30"),
            lambda c: c["events"][0].update(date="2026-10-13"),
            lambda c: c["events"][0]["evidence"][0].update(line=True),
            lambda c: c["events"][0]["evidence"][0].update(line=99),
            lambda c: c["events"][0]["evidence"][0].update(path="../secret.md"),
            lambda c: c["events"][0]["evidence"][0].update(path="/secret.md"),
            lambda c: c["events"][0].update(project_id="unknown"),
            lambda c: c["projects"][0].update(goal_status="confirmed", evidence=[]),
            lambda c: c["events"].append(copy.deepcopy(c["events"][0])),
        ]:
            candidate = self.candidate()
            transform(candidate)
            cases.append(candidate)
        for candidate in cases:
            with self.subTest(candidate=candidate):
                with self.assertRaises(HubError):
                    self.store.publish(candidate, packet["id"])
                self.assertFalse(self.store.current_path.exists())

    def test_event_date_must_appear_on_cited_line_not_elsewhere_in_document(self):
        self.source.write_text("# 当前状态\n\n2026-09-10：右拇指故障送修。\n2026-10-12：仅文档整理，尚未核验维修恢复。\n", encoding="utf-8")
        packet = self.store.prepare("baseline")
        candidate = self.candidate()
        candidate["events"][0].update(title="右拇指故障送修", detail="维修恢复待核验", kind="blocker")
        # The cited repair line says September 10; October 12 appears elsewhere only.
        with self.assertRaises(HubError):
            self.store.publish(candidate, packet["id"])
        self.assertFalse(self.store.current_path.exists())
        candidate["events"][0]["date"] = "2026-09-10"
        result = self.store.publish(candidate, packet["id"])
        self.assertEqual(result["events"][0]["date"], "2026-09-10")

    def test_source_race_and_new_files_reject_without_overwriting(self):
        first = self.publish()
        packet = self.store.prepare("daily")
        self.source.write_text(self.source.read_text() + "补充：现场验收未安排。\n", encoding="utf-8")
        with self.assertRaises(HubError) as caught:
            self.store.publish(self.candidate(), packet["id"])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.state()["snapshot"], first)
        packet = self.store.prepare("daily")
        (self.source.parent / "NEW.md").write_text("# 新记录\n", encoding="utf-8")
        with self.assertRaises(HubError):
            self.store.publish(self.candidate(), packet["id"])
        self.assertEqual(len(list((self.tracking / "history").glob("*.json"))), 1)

    def test_duplicate_publish_and_stale_revision_are_conflicts(self):
        first = self.store.prepare("baseline")
        parallel = self.store.prepare("baseline")
        self.store.publish(self.candidate(), first["id"])
        for packet in (first, parallel):
            with self.assertRaises(HubError) as caught:
                self.store.publish(self.candidate(), packet["id"])
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.state()["snapshot"]["revision"], 1)
        self.assertEqual(self.store.state()["pending_packets"], [])

    def test_concurrent_publish_has_one_winner_and_preserves_history(self):
        first = self.store.prepare("baseline")
        second = self.store.prepare("baseline")

        def publish_packet(packet):
            try:
                return self.store.publish(self.candidate(), packet["id"])["revision"]
            except HubError as error:
                return error.status

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(publish_packet, [first, second]))
        self.assertEqual(sorted(results), [1, 409])
        self.assertEqual(len(list((self.tracking / "history").glob("*.json"))), 1)

    def test_source_change_during_validation_is_checked_before_commit(self):
        prior = self.publish()
        packet = self.store.prepare("daily")
        validate = self.store._candidate

        def change_during_validation(*args):
            checked = validate(*args)
            self.source.write_text(self.source.read_text() + "审核过程中发生新变化。\n", encoding="utf-8")
            return checked

        with patch.object(self.store, "_candidate", side_effect=change_during_validation):
            with self.assertRaises(HubError) as caught:
                self.store.publish(self.candidate(), packet["id"])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(json.loads(self.store.current_path.read_text()), prior)
        self.assertEqual(len(list((self.tracking / "history").glob("*.json"))), 1)

    def test_primary_staleness_without_scan_and_secondary_after_scan(self):
        secondary = self.source.parent / "NOTES.md"
        secondary.write_text("# 相关记录\n", encoding="utf-8")
        first = self.publish()
        self.source.write_text(self.source.read_text() + "有新变化。\n", encoding="utf-8")
        state = self.store.state()
        self.assertTrue(state["freshness"]["stale"])
        self.assertIn("demo/docs/STATUS.md", state["freshness"]["changed_paths"])
        self.assertEqual(state["snapshot"], first)
        self.publish("update")
        secondary.unlink()
        self.hub.scan()
        self.assertIn("demo/docs/NOTES.md", self.store.state()["freshness"]["missing_paths"])

    def test_incremental_packet_retains_summary_and_reads_only_affected_sources(self):
        secondary = self.source.parent / "NOTES.md"
        secondary.write_text("# 相关记录\n", encoding="utf-8")
        initial = self.publish()
        packet = self.store.prepare("update")
        self.assertEqual(packet["sources"], [])
        self.assertEqual(packet["changed_paths"], [])
        self.assertEqual(packet["previous"]["projects"], initial["projects"])
        secondary.write_text("# 相关记录\n补充变化。\n", encoding="utf-8")
        packet = self.store.prepare("update")
        self.assertEqual({item["path"] for item in packet["sources"]}, {"demo/docs/NOTES.md", "demo/docs/STATUS.md"})
        self.assertEqual(packet["changes"][0]["kind"], "modified")

    def test_incremental_order_prioritizes_changed_primary_over_secondary(self):
        (self.workspace / "unknown").mkdir()
        (self.workspace / "unknown/STATUS.md").write_text("# 另一项目\n", encoding="utf-8")
        secondary = self.source.parent / "NOTES.md"
        secondary.write_text("# 相关记录\n", encoding="utf-8")
        self.publish()
        self.source.write_text(self.source.read_text() + "新的主入口记录。\n", encoding="utf-8")
        secondary.write_text("# 相关记录\n次级变化。\n", encoding="utf-8")
        packet = self.store.prepare("update")
        self.assertEqual([item["path"] for item in packet["sources"]], ["demo/docs/STATUS.md", "demo/docs/NOTES.md"])
        self.assertIn({"path": "unknown/STATUS.md", "reason": "unchanged_primary",
                       "sha256": packet["source_manifest"]["unknown/STATUS.md"]["sha256"]}, packet["unread"])

    def test_daily_weekly_boundaries_use_shanghai_and_preserve_event_dates(self):
        self.set_timezone("Asia/Shanghai")
        timestamp = dt.datetime(2026, 10, 16, 0, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
        with patch("review_store.local_now", return_value=timestamp):
            daily = self.publish("daily")
            weekly = self.publish("weekly")
        self.assertEqual(daily["period_start"], "2026-10-16")
        self.assertEqual(daily["period_end"], "2026-10-16")
        self.assertEqual(weekly["period_start"], "2026-10-12")
        self.assertEqual(weekly["period_end"], "2026-10-16")
        self.assertEqual(daily["events"][0]["date"], "2026-10-12")
        self.assertTrue(daily["reviewed_at"].endswith("+08:00"))
        self.assertEqual(daily["timezone"], "Asia/Shanghai")
        self.assertEqual(daily["report_scope"], "baseline")

    def test_reports_remain_in_prepared_period_across_midnight_and_week_boundary(self):
        self.set_timezone("Asia/Shanghai")
        prepared_at = dt.datetime(2026, 10, 18, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
        published_at = dt.datetime(2026, 10, 19, 0, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
        for mode in ("daily", "weekly"):
            with self.subTest(mode=mode):
                with patch("review_store.local_now", return_value=prepared_at):
                    packet = self.store.prepare(mode)
                self.assertEqual(packet["prepared_at"], packet["created_at"])
                with patch("review_store.local_now", return_value=published_at):
                    snapshot = self.store.publish(self.candidate(), packet["id"])
                self.assertEqual(snapshot["period_start"], "2026-10-12" if mode == "weekly" else "2026-10-18")
                self.assertEqual(snapshot["period_end"], "2026-10-18")
                self.assertEqual(snapshot["period_start"], packet["period_start"])
                self.assertEqual(snapshot["period_end"], packet["period_end"])
                self.assertTrue(snapshot["reviewed_at"].startswith("2026-10-19T00:01"))

    def test_new_york_default_uses_local_day_and_keeps_week_across_midnight(self):
        # UTC is already Monday, while New York is still Sunday after the DST change.
        prepared_at = dt.datetime(2026, 11, 2, 4, 30, tzinfo=dt.timezone.utc)
        published_at = dt.datetime(2026, 11, 2, 5, 1, tzinfo=dt.timezone.utc)
        self.assertFalse(self.preferences.exists())
        with patch("review_store.local_now", return_value=prepared_at):
            packet = self.store.prepare("weekly")
        with patch("review_store.local_now", return_value=published_at):
            snapshot = self.store.publish(self.candidate(), packet["id"])
        self.assertEqual(self.store.report_timezone, "America/New_York")
        self.assertEqual(packet["timezone"], "America/New_York")
        self.assertEqual(packet["created_at"], "2026-11-01T23:30:00-05:00")
        self.assertEqual(snapshot["period_start"], "2026-10-26")
        self.assertEqual(snapshot["period_end"], "2026-11-01")
        self.assertEqual(snapshot["reviewed_at"], "2026-11-02T00:01:00-05:00")
        self.assertEqual(snapshot["latest_reports"]["weekly"]["timezone"], "America/New_York")
        self.assertEqual(self.store.state()["timezone"], "America/New_York")

    def test_new_york_dst_repeated_hour_preserves_day_and_real_publication_offset(self):
        self.set_timezone("America/New_York")
        prepared_at = dt.datetime(2026, 11, 1, 5, 30, tzinfo=dt.timezone.utc)
        published_at = dt.datetime(2026, 11, 1, 6, 30, tzinfo=dt.timezone.utc)
        with patch("review_store.local_now", return_value=prepared_at):
            packet = self.store.prepare("daily")
        with patch("review_store.local_now", return_value=published_at):
            snapshot = self.store.publish(self.candidate(), packet["id"])
        self.assertEqual(packet["created_at"], "2026-11-01T01:30:00-04:00")
        self.assertEqual(snapshot["reviewed_at"], "2026-11-01T01:30:00-05:00")
        self.assertEqual(snapshot["period_start"], "2026-11-01")
        self.assertEqual(snapshot["period_end"], "2026-11-01")

    def test_timezone_change_marks_stale_and_rejects_pending_review(self):
        original = self.publish()
        packet = self.store.prepare("daily")
        self.set_timezone("Asia/Shanghai")
        state = self.store.state()
        self.assertEqual(state["timezone"], "Asia/Shanghai")
        self.assertTrue(state["freshness"]["timezone_changed"])
        self.assertTrue(state["freshness"]["stale"])
        with self.assertRaises(HubError) as caught:
            self.store.publish(self.candidate(), packet["id"])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.state()["snapshot"], original)
        replacement = self.publish("daily")
        self.assertEqual(replacement["timezone"], "Asia/Shanghai")
        self.assertFalse(self.store.state()["freshness"]["timezone_changed"])

    def test_tracking_change_marks_stale_and_rejects_pending_review(self):
        first = self.publish()
        packet = self.store.prepare("daily")
        self.assertEqual(packet["tracking_preferences"]["demo"]["tier"], "secondary")
        self.hub.update_tracking("demo", {"tier": "main", "archived": True})
        state = self.store.state()
        self.assertTrue(state["freshness"]["preferences_changed"])
        self.assertTrue(state["freshness"]["stale"])
        with self.assertRaises(HubError) as caught:
            self.store.publish(self.candidate(), packet["id"])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.state()["snapshot"], first)
        fresh_packet = self.store.prepare("update")
        self.assertNotEqual(fresh_packet["tracking_fingerprint"], packet["tracking_fingerprint"])
        self.store.publish(self.candidate(), fresh_packet["id"])
        self.assertFalse(self.store.state()["freshness"]["stale"])
        preferences = self.hub.tracking_preferences()["demo"]
        self.assertEqual(preferences["tier"], "main")
        self.assertTrue(preferences["archived"])
        self.hub.update_tracking("demo", {"tier": "main", "archived": True})
        self.assertFalse(self.store.state()["freshness"]["preferences_changed"])

    def test_tracking_change_during_validation_is_checked_before_commit(self):
        self.publish()
        packet = self.store.prepare("daily")
        validate = self.store._candidate

        def change_tracking(*args):
            result = validate(*args)
            self.hub.update_tracking("demo", {"archived": True})
            return result

        with patch.object(self.store, "_candidate", side_effect=change_tracking):
            with self.assertRaises(HubError) as caught:
                self.store.publish(self.candidate(), packet["id"])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.state()["snapshot"]["revision"], 1)

    def test_daily_and_weekly_reports_survive_following_updates(self):
        daily = self.publish("daily")
        weekly = self.publish("weekly")
        updated = self.publish("update")
        self.assertEqual(set(updated["latest_reports"]), {"daily", "weekly"})
        self.assertEqual(updated["latest_reports"]["daily"]["revision"], daily["revision"])
        self.assertEqual(updated["latest_reports"]["weekly"]["revision"], weekly["revision"])
        self.assertEqual(updated["latest_reports"]["daily"]["report"], daily["report"])
        self.assertNotIn("latest_reports", updated["latest_reports"]["daily"])
        self.assertNotIn("source_manifest", updated["latest_reports"]["weekly"])

    def test_missing_primary_can_remain_unknown_but_new_file_invalidates_packet(self):
        packet = self.store.prepare("baseline")
        (self.workspace / "unknown").mkdir()
        (self.workspace / "unknown/STATUS.md").write_text("# 补全状态\n", encoding="utf-8")
        result = self.store.publish(self.candidate(), packet['id'])
        self.assertEqual(result['revision'], 1)
        self.assertIn('unknown/STATUS.md', self.store.state()['freshness']['changed_paths'])

    def test_report_read_generation_dedup_and_homepage_isolation(self):
        first = self.publish()
        with patch.object(self.hub, 'scan', side_effect=AssertionError('report must not scan')):
            self.assertIsNone(self.store.read_report('daily', '2026-10-12')['report'])
            packet = self.store.prepare_report('daily', '2026-10-12')
            result = self.store.publish_report(self.candidate(), packet['id'])
            self.assertEqual(result['base_revision'], first['revision'])
            self.assertEqual(self.store.cached_report('daily', '2026-10-12'), result)
            self.assertIsNone(self.store.read_report('daily', '2026-10-13')['report'])
            self.assertIsNone(self.store.read_report('weekly', '2026-10-12')['report'])
            again = self.store.prepare_report('daily', '2026-10-12')
            self.assertEqual(self.store.publish_report(self.candidate(), again['id']), result)
        self.assertEqual(json.loads(self.store.current_path.read_text()), first)
        self.assertEqual(len(list((self.tracking/'history').glob('*.json'))), 1)

    def test_report_empty_period_and_inflight_memory_version(self):
        first = self.publish()
        packet = self.store.prepare_report('daily', '2026-01-01')
        self.publish('update')
        result = self.store.publish_report(self.candidate(), packet['id'])
        self.assertEqual(result['base_revision'], first['revision'])
        self.assertEqual(result['report']['changes'], [])
        self.assertEqual(result['report']['headline'], '暂无新进展')
        self.assertIsNone(self.store.cached_report('daily', '2026-01-01'))
        self.assertEqual(self.store.state()['snapshot']['revision'], 2)

    def test_cli_isolated_prepare_publish_and_status(self):
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / "review.py"),
                   "--workspace", str(self.workspace), "--runtime", str(self.hub.runtime),
                   "--config", str(self.config), "--tracking-root", str(self.tracking), "--compact"]
        prepared = subprocess.run(command + ["prepare", "--mode", "baseline"], capture_output=True, text=True, check=True)
        packet = json.loads(prepared.stdout)
        candidate_path = self.workspace / "candidate.json"
        candidate_path.write_text(json.dumps(self.candidate()), encoding="utf-8")
        published = subprocess.run(command + ["publish", "--packet", packet["id"], "--candidate", str(candidate_path)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(published.stdout)["revision"], 1)
        status = subprocess.run(command + ["status"], capture_output=True, text=True, check=True)
        self.assertFalse(json.loads(status.stdout)["freshness"]["stale"])


if __name__ == "__main__":
    unittest.main()

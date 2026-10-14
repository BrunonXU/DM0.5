import hashlib
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hub import Hub, HubError, parse_document
from server import HubServer


class HubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        (self.workspace / "demo/docs").mkdir(parents=True)
        self.source = self.workspace / "demo/docs/STATUS.md"
        self.source.write_text("# 当前状态\n\n文档整理：2026-10-12；最后现场记录仍为2026-09-10\n\n- [ ] 核验实验\n", encoding="utf-8")
        self.config = self.workspace / "projects.json"
        self.config.write_text(json.dumps({"schema_version": 1, "projects": [{"id": "demo", "name": "Demo", "source_paths": ["demo/docs/STATUS.md"], "watch_paths": ["demo/docs"], "milestones": []}]}), encoding="utf-8")
        self.hub = self.make_hub()

    def make_hub(self):
        return Hub(self.workspace, self.workspace / "runtime", self.config)

    def test_incremental_scan_restart_and_removal(self):
        self.hub.scan()
        state = self.hub.state()
        self.assertEqual(state["changes"][0]["kind"], "indexed")
        self.assertEqual(state["projects"][0]["source_date"], "2026-10-12")
        self.assertEqual(state["projects"][0]["evidence_date"], "2026-09-10")
        original_id = state["tasks"][0]["id"]
        self.hub.update_task(original_id, {"pinned": True, "priority": "P1"})
        self.source.write_text("# 新标题\n\n- [ ] 核验实验\n\n新的记录\n", encoding="utf-8")
        self.hub = self.make_hub()
        self.hub.scan()
        state = self.hub.state()
        self.assertEqual(state["tasks"][0]["id"], original_id)
        self.assertTrue(state["tasks"][0]["pinned"])
        self.assertEqual(state["tasks"][0]["priority"], "P1")
        self.assertIn("modified", [c["kind"] for c in state["changes"]])
        self.source.unlink()
        self.hub.scan()
        state = self.hub.state()
        self.assertEqual(state["tasks"], [])
        self.assertEqual(state["sync"]["document_count"], 0)
        self.assertIn("removed", [c["kind"] for c in state["changes"]])

    def test_change_evidence_summary_briefing_and_diff_limits(self):
        original = "---\nupdated: 2026-10-11\n---\n# 实验状态\n\n旧结论：相机同步未通过。\n"
        updated = "---\nupdated: 2026-10-12\n---\n# 实验状态\n\n新结论：相机同步已通过，动作映射待核验。\n"
        self.source.write_text(original, encoding="utf-8")
        self.hub.scan()
        initial = self.hub.state()["changes"][0]
        self.assertEqual(initial["diff"], "")
        self.assertFalse(initial["diff_truncated"])
        self.source.write_text(updated, encoding="utf-8")
        self.hub.scan()
        change = self.hub.briefing()["recent_changes"][0]
        self.assertIn("-旧结论：相机同步未通过。", change["diff"])
        self.assertIn("+新结论：相机同步已通过，动作映射待核验。", change["diff"])
        self.assertIn("@@", change["diff"])
        self.assertTrue(change["summary"].startswith("规则摘录："))
        self.assertIn("新结论", change["summary"])
        self.assertIn("旧结论", change["summary"])
        self.assertNotIn("updated:", change["summary"])
        self.assertFalse(change["diff_truncated"])
        self.source.unlink()
        self.hub.scan()
        removed = self.hub.briefing()["recent_changes"][0]
        self.assertEqual(removed["kind"], "removed")
        self.assertIn("-新结论", removed["diff"])
        self.assertIn("删除前：新结论", removed["summary"])
        self.source.write_text("# 长文档\n" + "\n".join(f"新增证据 {n}" for n in range(250)), encoding="utf-8")
        self.hub.scan()
        many_lines = self.hub.briefing()["recent_changes"][0]
        self.assertTrue(many_lines["diff_truncated"])
        self.assertLessEqual(len(many_lines["diff"].splitlines()), 120)
        self.source.write_text("# 长文档\n" + "新增长记录" * 2000, encoding="utf-8")
        self.hub.scan()
        # Replace a single very long line to exercise the character cap independently
        # of the separate 120-line limit above.
        self.source.write_text("# 长文档\n" + "另一份长记录" * 2000, encoding="utf-8")
        self.hub.scan()
        large = self.hub.briefing()["recent_changes"][0]
        self.assertTrue(large["diff_truncated"])
        self.assertEqual(len(large["diff"]), 8000)
        self.assertLessEqual(len(large["diff"].splitlines()), 120)
        self.hub = self.make_hub()
        self.assertEqual(self.hub.briefing()["recent_changes"][0]["diff"], large["diff"])

    def test_frozen_historical_and_code_checkboxes_not_imported(self):
        for content in ["---\nstatus: frozen\n---\n# 冻结\n- [ ] 旧任务\n", "---\nstatus: draft\n---\n- [ ] 草稿任务\n", "---\nstatus: proposed\n---\n- [ ] 提案任务\n", "# 状态\n下文是历史快照\n- [ ] 旧任务\n", "# 示例\n```md\n- [ ] 示例任务\n```\n"]:
            self.source.write_text(content, encoding="utf-8")
            self.hub.scan()
            self.assertEqual(self.hub.state()["tasks"], [])
        (self.workspace / "demo/docs/extra.md").write_text("- [ ] 非权威文档任务\n", encoding="utf-8")
        self.hub.scan()
        self.assertEqual(self.hub.state()["tasks"], [])

    def test_historical_subsections_and_current_scope(self):
        self.source.write_text("# 状态\n## 历史记录\n- [ ] 旧待办\n### 当时的下一步\n- [ ] 更旧任务\n## 当前计划\n- [ ] 有效任务\n## 模板\n- [ ] 示例任务\n", encoding="utf-8")
        self.hub.scan()
        self.assertEqual([t["title"] for t in self.hub.state()["tasks"]], ["有效任务"])

    def test_date_boundaries_current_summary_and_dynamic_milestones(self):
        parsed = parse_document("docs/status.md", "---\nevidence_date: 2026-09-10\n---\n# 状态\n")
        self.assertIsNone(parsed["updated_date"])
        self.assertEqual(parsed["evidence_date"], "2026-09-10")
        self.source.write_text("# 项目\n更新：2026-10-12\n\n## 目标\n宏大的目标\n## 里程碑\n1. 采集数据\n- [x] 冻结协议\n## 当前状态\n正在核验相机同步。\n", encoding="utf-8")
        self.hub.scan()
        project = self.hub.state()["projects"][0]
        self.assertEqual(project["summary"], "正在核验相机同步。")
        self.assertEqual(project["source_date"], "2026-10-12")
        self.assertEqual([m["status"] for m in project["milestones"]], ["planned", "done"])
        self.source.write_text("# 项目\n## 当前状态\n更改计划。\n", encoding="utf-8")
        self.hub.scan()
        project = self.hub.state()["projects"][0]
        self.assertEqual(project["status"], "登记状态待复核")
        self.assertIsNone(project["evidence_date"])
        self.assertTrue(any("登记依据发生变化" in warning for warning in project["warnings"]))

    def test_source_checkbox_conflict_and_exact_write(self):
        self.hub.scan()
        task = self.hub.state()["tasks"][0]
        original = self.source.read_text(encoding="utf-8")
        self.source.write_text(original + "\n外部并发改动\n", encoding="utf-8")
        with self.assertRaises(HubError) as caught:
            self.hub.update_task(task["id"], {"status": "done", "source_hash": task["source_hash"]})
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("外部并发改动", self.source.read_text(encoding="utf-8"))
        self.hub.scan()
        task = self.hub.state()["tasks"][0]
        before = self.source.read_text(encoding="utf-8")
        self.hub.update_task(task["id"], {"status": "done", "source_hash": task["source_hash"]})
        self.assertEqual(self.source.read_text(encoding="utf-8"), before.replace("- [ ]", "- [x]"))
        self.assertEqual(self.hub.state()["tasks"][0]["status"], "done")

    def test_timer_durability_and_manual_time_timezone(self):
        task = self.hub.add_task({"project_id": "demo", "title": "实验"})
        self.hub.start_timer({"task_id": task["id"]})
        self.hub = self.make_hub()
        self.assertEqual(self.hub.state()["timer"]["task_id"], task["id"])
        with self.assertRaises(HubError):
            self.hub.start_timer({"project_id": "demo"})
        self.hub.stop_timer()
        self.assertIsNone(self.hub.state()["timer"])
        entry = self.hub.add_time({"project_id": "demo", "date": "2026-10-12", "minutes": 12, "kind": "compute"})
        self.assertEqual(entry["duration_seconds"], 720)
        self.assertTrue(entry["started_at"].endswith("+08:00"))

    def test_document_access_traversal_and_symlinks(self):
        for path in ["../secrets.md", "/etc/passwd", "projects.json", "other/file.md"]:
            with self.assertRaises(HubError):
                self.hub.document(path)
        outside = self.workspace / "secret.md"
        outside.write_text("secret", encoding="utf-8")
        (self.workspace / "demo/docs/link.md").symlink_to(outside)
        with self.assertRaises(HubError):
            self.hub.document("demo/docs/link.md")
        self.hub.scan()
        self.assertEqual(self.hub.state()["sync"]["document_count"], 1)

    def test_temporary_read_error_preserves_last_good_snapshot(self):
        self.hub.scan()
        original_read = self.hub.read_text
        self.hub.read_text = lambda path: (_ for _ in ()).throw(PermissionError("temporary"))
        self.hub.scan()
        self.assertEqual(len(self.hub.state()["tasks"]), 1)
        self.assertEqual(self.hub.state()["sync"]["document_count"], 1)
        self.assertIsNotNone(self.hub.state()["sync"]["error"])
        self.hub.read_text = original_read
        self.hub.scan()
        self.assertIsNone(self.hub.state()["sync"]["error"])

    def test_duplicate_checkboxes_skipped_and_manual_survives_sync(self):
        manual = self.hub.add_task({"project_id": "demo", "title": "保留我的任务", "pinned": True})
        self.source.write_text("# 状态\n- [ ] 重复\n- [ ] 重复\n", encoding="utf-8")
        self.hub.scan()
        self.assertEqual([t["id"] for t in self.hub.state()["tasks"]], [manual["id"]])

    def test_task_belongs_to_canonical_owner_not_broad_directory_watcher(self):
        config = json.loads(self.config.read_text())
        config["projects"].insert(0, {"id": "aaa-observer", "name": "Shared documents", "source_paths": [], "watch_paths": ["demo/docs"]})
        self.config.write_text(json.dumps(config))
        self.hub.scan()
        self.assertEqual(self.hub.state()["tasks"][0]["project_id"], "demo")

    def test_http_security_and_health_do_not_start_timer(self):
        self.hub.scan()
        web = self.workspace / "web"
        web.mkdir()
        (web / "index.html").write_text("<!doctype html><title>Hub</title>", encoding="utf-8")
        server = HubServer(("127.0.0.1", 0), self.hub, web)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(method, path, body=None, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            result = response.status, response.read()
            connection.close()
            return result
        self.assertEqual(request("GET", "/api/state")[0], 200)
        self.assertIsNone(self.hub.state()["timer"])
        self.assertEqual(request("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(request("GET", "/%2e%2e/projects.json")[0], 403)
        headers = {"Content-Type": "application/json", "Origin": "https://evil.example"}
        self.assertEqual(request("POST", "/api/tasks", "{}", headers)[0], 403)
        self.assertEqual(request("POST", "/api/tasks", "{}")[0], 415)
        self.assertEqual(request("GET", "/")[0], 200)


if __name__ == "__main__":
    unittest.main()

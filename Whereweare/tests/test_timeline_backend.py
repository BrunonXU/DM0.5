import http.client
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hub import Hub, HubError
from server import HubServer


class TimelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.runtime = self.workspace / "runtime"
        self.config = self.workspace / "projects.json"
        self.config.write_text(json.dumps({"projects": [
            {"id": name, "name": name, "source_paths": [], "watch_paths": []}
            for name in ("bottles", "blocks")
        ]}), encoding="utf-8")
        self.hub = self.make_hub()

    def make_hub(self):
        return Hub(self.workspace, self.runtime, self.config)

    def add(self, **fields):
        return self.hub.add_timeline_node({"project_id": "bottles", "title": "实验", **fields})

    def assert_error(self, status, operation, *args):
        with self.assertRaises(HubError) as caught:
            operation(*args)
        self.assertEqual(caught.exception.status, status)

    def test_persists_tree_through_restart_and_scan(self):
        root = self.add(title="基线", date="2026-10-12", status="done")
        child = self.add(title="改进", parent_id=root["id"], status="active", notes="  核验抓取  ")
        sibling = self.add(title="新实验", parent_id=root["id"])
        self.assertIsNone(child["date"])
        self.assertEqual(child["notes"], "核验抓取")
        self.assertEqual(sibling["status"], "planned")
        self.hub = self.make_hub()
        self.hub.scan()
        self.assertEqual(self.hub.state()["timeline_nodes"], [root, child, sibling])
        self.assertEqual(self.hub.briefing()["timeline_nodes"], [root, child, sibling])
        updated = self.hub.update_timeline_node(child["id"], {"title": "真机验收", "status": "blocked", "date": "2026-10-14", "notes": "等设备"})
        self.assertEqual(updated["created_at"], child["created_at"])
        self.assertGreater(updated["updated_at"], child["updated_at"])
        self.hub = self.make_hub()
        self.assertEqual(self.hub.state()["timeline_nodes"][1], updated)

    def test_accepts_only_explicit_valid_dates(self):
        for value in ("", "2026-02-29", "20261012", "2026-1-1", "2026-10-12T00:00:00", 0, False, [], {}):
            with self.subTest(value=value):
                self.assert_error(400, self.hub.add_timeline_node, {"project_id": "bottles", "title": "实验", "date": value})
        leap = self.add(date="2028-02-29")
        cleared = self.hub.update_timeline_node(leap["id"], {"date": None})
        self.assertIsNone(cleared["date"])

    def test_parent_dates_and_failed_edits_are_atomic(self):
        root = self.add(date="2026-10-12")
        self.assert_error(409, self.hub.add_timeline_node, {"project_id": "bottles", "title": "过早", "parent_id": root["id"], "date": "2026-10-11"})
        child = self.add(parent_id=root["id"], date="2026-10-13")
        self.assert_error(409, self.hub.update_timeline_node, child["id"], {"date": "2026-10-11"})
        self.assert_error(409, self.hub.update_timeline_node, root["id"], {"date": "2026-10-14", "title": "不能部分写入"})
        self.assertEqual(self.hub.state()["timeline_nodes"], [root, child])
        self.hub.update_timeline_node(root["id"], {"date": "2026-10-13"})
        later_root = self.add(date="2026-10-15")
        self.assert_error(409, self.hub.update_timeline_node, child["id"], {"parent_id": later_root["id"]})
        self.hub.update_timeline_node(child["id"], {"parent_id": None, "date": "2026-10-10"})

    def test_parent_must_exist_and_belong_to_same_project(self):
        foreign = self.add(project_id="blocks")
        self.assert_error(409, self.hub.add_timeline_node, {"project_id": "bottles", "title": "错误项目", "parent_id": foreign["id"]})
        self.assert_error(404, self.hub.add_timeline_node, {"project_id": "bottles", "title": "虚拟节点", "parent_id": "milestone_virtual"})
        root = self.add()
        self.assert_error(409, self.hub.update_timeline_node, root["id"], {"parent_id": foreign["id"]})
        self.assert_error(400, self.hub.update_timeline_node, root["id"], {"project_id": "blocks"})
        self.assert_error(404, self.hub.update_timeline_node, "missing", {"title": "不存在"})

    def test_dates_respect_ancestors_across_undated_nodes(self):
        root = self.add(date="2026-10-12")
        undated = self.add(parent_id=root["id"])
        self.assert_error(409, self.hub.add_timeline_node, {"project_id": "bottles", "title": "过早", "parent_id": undated["id"], "date": "2026-10-11"})
        leaf = self.add(parent_id=undated["id"], date="2026-10-14")
        self.assert_error(409, self.hub.update_timeline_node, root["id"], {"date": "2026-10-15"})
        self.assert_error(409, self.hub.update_timeline_node, leaf["id"], {"date": "2026-10-11"})
        later_root = self.add(date="2026-10-16")
        # Moving an undated subtree must also preserve dated descendant order.
        self.assert_error(409, self.hub.update_timeline_node, undated["id"], {"parent_id": later_root["id"]})

    def test_cycles_are_rejected_and_branch_can_be_detached(self):
        root = self.add()
        child = self.add(parent_id=root["id"])
        grandchild = self.add(parent_id=child["id"])
        self.assert_error(409, self.hub.update_timeline_node, root["id"], {"parent_id": root["id"]})
        self.assert_error(409, self.hub.update_timeline_node, root["id"], {"parent_id": grandchild["id"]})
        self.assert_error(409, self.hub.update_timeline_node, child["id"], {"parent_id": grandchild["id"]})
        detached = self.hub.update_timeline_node(child["id"], {"parent_id": None})
        self.assertIsNone(detached["parent_id"])
        self.assertEqual(self.hub.update_timeline_node(root["id"], {"parent_id": grandchild["id"]})["parent_id"], grandchild["id"])

    def test_fields_are_validated(self):
        for fields in ({"title": " "}, {"title": None}, {"title": "x" * 501}, {"status": "doing"}, {"status": []},
                       {"notes": "x" * 5001}, {"notes": None}, {"parent_id": ""}, {"parent_id": {}},
                       {"project_id": []}, {"created_at": "2026-10-12"}):
            with self.subTest(fields=fields):
                self.assert_error(400, self.hub.add_timeline_node, {"project_id": "bottles", "title": "实验", **fields})
        self.assert_error(400, self.hub.add_timeline_node, {"project_id": "bottles"})
        self.assert_error(404, self.hub.add_timeline_node, {"project_id": "missing", "title": "实验"})
        self.assertEqual(self.hub.state()["timeline_nodes"], [])

    def test_additive_schema_preserves_existing_data(self):
        task = self.hub.add_task({"project_id": "bottles", "title": "保留任务"})
        # Simulate an existing installation before the timeline table was added.
        with sqlite3.connect(self.hub.db_path) as conn:
            conn.execute("DROP TABLE timeline_nodes")
        self.hub = self.make_hub()
        self.assertEqual(self.hub.state()["tasks"], [task])
        self.assertEqual(self.hub.state()["timeline_nodes"], [])
        self.add()
        self.assertEqual(len(self.hub.state()["timeline_nodes"]), 1)

    def test_http_create_update_and_state(self):
        server = HubServer(("127.0.0.1", 0), self.hub)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def request(method, path, data=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request(method, path, body=json.dumps(data) if data is not None else None,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            result = response.status, json.loads(response.read())
            connection.close()
            return result

        status, root = request("POST", "/api/timeline/nodes", {"project_id": "bottles", "title": "基线"})
        self.assertEqual(status, 201)
        status, updated = request("PATCH", "/api/timeline/nodes/" + root["id"], {"status": "done"})
        self.assertEqual(status, 200)
        self.assertEqual(updated["status"], "done")
        self.assertEqual(request("GET", "/api/state")[1]["timeline_nodes"], [updated])
        self.assertEqual(request("GET", "/api/briefing")[1]["timeline_nodes"], [updated])
        self.assertEqual(request("PATCH", "/api/timeline/nodes/" + root["id"], {"parent_id": root["id"]})[0], 409)
        self.assertEqual(request("PATCH", "/api/timeline/nodes/missing", {"status": "done"})[0], 404)
        self.assertEqual(request("PATCH", "/api/timeline/nodes/", {"status": "done"})[0], 400)


if __name__ == "__main__":
    unittest.main()

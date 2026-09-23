from pathlib import Path
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pyarrow.parquet as pq

import test_lerobot_v3 as fixture_module
from pico_f1.lerobot_v3 import LeRobotV3Dataset
from pico_f1.transfer import enqueue, read, merge, outbox_index, acknowledge, object_hash, destination_path, receive_robot


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.dest = self.base / "central/mm/mocap_data"
        self.allowed = self.base / "central"
        self.allowed.mkdir()

    def source(self, robot="F1_D070", task="synthetic"):
        project = self.base / robot
        root = project / "mocap_data"
        fixture = fixture_module.LeRobotV3WriterTest()
        staging, arrays, camera_info = fixture._episode(root, 0)
        LeRobotV3Dataset(root, 30, task).commit(staging, arrays, camera_info)
        enqueue(project, root, robot, self.dest)
        item = outbox_index(project)[0]
        bundle = project / "transfer_state/outbox" / item["key"]
        return project, bundle / "payload", read(bundle / "manifest.json")

    def test_two_robots_same_episode_and_retry(self):
        p0, payload0, m0 = self.source()
        _, payload1, m1 = self.source("F1_D072", "another task")
        first = merge(payload0, m0, self.allowed)
        second = merge(payload1, m1, self.allowed)
        self.assertEqual((first["global_episode"], second["global_episode"]), (0, 1))

        self.assertEqual(merge(payload0, m0, self.allowed), first)
        info = read(self.dest / "meta/info.json")
        self.assertEqual((info["total_episodes"], info["total_frames"], info["total_tasks"]), (2, 4, 2))
        table = pq.read_table(self.dest / "data/chunk-000/file-001.parquet")
        self.assertEqual(table["episode_index"].to_pylist(), [1, 1])
        self.assertEqual(table["index"].to_pylist(), [2, 3])
        self.assertEqual(table["task_index"].to_pylist(), [1, 1])
        original = pq.read_table(payload1 / "data/chunk-000/file-000.parquet")
        for name in original.column_names:
            if name not in ("episode_index", "index", "task_index"):
                self.assertEqual(table[name], original[name])
        acknowledge(p0, first)
        self.assertEqual(outbox_index(p0), [])

    def test_corruption_never_published(self):
        _, payload, manifest = self.source()
        (payload / manifest["files"][0]["path"]).write_bytes(b"broken")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            merge(payload, manifest, self.allowed)
        self.assertFalse(self.dest.exists())

    def test_scene_reset_restarts_numbering_and_blocks_old_outbox(self):
        from pico_f1.scene_reset import plan, rotate
        _, old_payload, old = self.source()
        merge(old_payload, old, self.allowed)
        central_project = self.base/'server/pico'
        central_project.mkdir(parents=True)
        rotate(central_project, self.allowed, 'mm', plan(central_project, self.allowed, 'mm'))
        with self.assertRaisesRegex(ValueError, 'SCENE_RESET_OLD_BATCH'):
            merge(old_payload, old, self.allowed)
        _, new_payload, new = self.source('F1_D072')
        result = merge(new_payload, new, self.allowed)
        self.assertEqual(result['global_episode'], 0)
        rows = pq.read_table(self.dest/'data/chunk-000/file-000.parquet')
        self.assertEqual(rows['index'].to_pylist(), [0, 1])
        self.assertEqual(read(self.dest/'meta/info.json')['total_episodes'], 1)

    def test_d070_01_distinct_provenance_from_d070(self):
        _, old_payload, old = self.source('F1_D070')
        _, new_payload, new = self.source('F1_D070_01')
        self.assertNotEqual(old['key'], new['key'])
        first = merge(old_payload, old, self.allowed)
        second = merge(new_payload, new, self.allowed)
        self.assertEqual((first['global_episode'], second['global_episode']), (0, 1))
        self.assertEqual((first['robot_id'], second['robot_id']), ('F1_D070', 'F1_D070_01'))

    def test_crash_before_and_after_publish(self):
        _, payload, manifest = self.source()
        with self.assertRaises(RuntimeError):
            merge(payload, manifest, self.allowed, fail_at="before_publish")
        self.assertFalse(self.dest.exists())
        with self.assertRaises(RuntimeError):
            merge(payload, manifest, self.allowed, fail_at="after_publish")
        self.assertEqual(read(self.dest / "meta/info.json")["total_episodes"], 1)
        self.assertEqual(merge(payload, manifest, self.allowed)["global_episode"], 0)

    def test_scene_change_preserves_queued_destination(self):
        project, _, manifest = self.source()
        enqueue(project, project / "mocap_data", "F1_D070", self.allowed / "other/mocap_data")
        path = project / "transfer_state/outbox" / manifest["key"] / "manifest.json"
        self.assertEqual(read(path)["destination"], str(self.dest))

    def test_uncommitted_metadata_not_queued(self):
        project, _, manifest = self.source()
        info = project / "mocap_data/meta/info.json"
        original = read(info)
        original.update(total_episodes=0, total_frames=0)
        info.write_text(json.dumps(original))
        self.assertEqual(enqueue(project, project / "mocap_data", "F1_D070", self.dest), [])

    def test_foreign_directory_and_path_escape_rejected(self):
        _, payload, manifest = self.source()
        self.dest.mkdir(parents=True)
        (self.dest / "user-data.txt").write_text("do not overwrite")
        with self.assertRaisesRegex(ValueError, "unmanaged"):
            merge(payload, manifest, self.allowed)
        with self.assertRaises(ValueError):
            destination_path("/etc/mm/mocap_data", self.allowed)
        outside = self.base / "outside"
        outside.mkdir()
        (self.allowed / "redirect").symlink_to(outside)
        with self.assertRaises(ValueError):
            destination_path(str(self.allowed / "redirect/mocap_data"), self.allowed)

    def test_identity_conflict_rejected(self):
        _, payload, manifest = self.source()
        merge(payload, manifest, self.allowed)
        manifest["queued_at"] += 1
        with self.assertRaisesRegex(ValueError, "different content"):
            merge(payload, manifest, self.allowed)

    def test_parallel_robots_allocate_once(self):
        _, p0, m0 = self.source()
        _, p1, m1 = self.source("F1_D071")
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(merge, p, m, self.allowed) for p, m in ((p0, m0), (p1, m1))]
            self.assertEqual(sorted(f.result()["global_episode"] for f in futures), [0, 1])
        self.assertEqual(len(read(self.dest / "provenance.json")["episodes"]), 2)

    def test_changed_contract_does_not_modify_current(self):
        _, p0, m0 = self.source()
        _, p1, m1 = self.source("F1_D072")
        merge(p0, m0, self.allowed)
        previous = self.dest.resolve()
        m1["info"]["features"]["action"]["names"][0] = "wrong_motor"
        with self.assertRaisesRegex(ValueError, "incompatible"):
            merge(p1, m1, self.allowed)
        self.assertEqual(self.dest.resolve(), previous)

    def test_bad_ack_does_not_clear_outbox(self):
        project, payload, manifest = self.source()
        receipt = merge(payload, manifest, self.allowed)
        receipt["manifest_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            acknowledge(project, receipt)
        self.assertEqual(len(outbox_index(project)), 1)

    def test_failed_episode_does_not_block_later_episode(self):
        with patch("pico_f1.transfer.ssh", return_value=b'[{"key":"bad"},{"key":"good"}]'), \
             patch("pico_f1.transfer.receive_item", side_effect=[ValueError("broken"), None]) as receive:
            with self.assertRaisesRegex(ValueError, "已完成1条"):
                receive_robot("F1_D070", self.base, self.allowed)
            self.assertEqual(receive.call_count, 2)


if __name__ == "__main__":
    unittest.main()

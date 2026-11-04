"""Causality, provenance, immutable-source and payload-integrity checks."""
import hashlib
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

import timing_audit as audit


def observation(index, receipts, available):
    return {"observation_id": index, "available_monotonic_ns": available,
        "frames": [{"camera": c, "frame_id": index, "host_received_monotonic_ns": t,
            "timestamp": {"unit": "ns", "value": t + 1000, "clock_domain": "unverified"},
            "path": f"{c}.pack", "payload_offset": index, "payload_bytes": 1,
            "payload_sha256": "fake-for-selection-only"}
            for c, t in zip(audit.CAMERAS, receipts)]}


class TimingAuditTest(unittest.TestCase):
    def test_statistics_include_p99_and_explicit_empty_values(self):
        result = audit.stats([40, 0, 20, 30, 10])
        self.assertEqual(result["p50"], 20)
        self.assertEqual(result["p95"], 38)
        self.assertAlmostEqual(result["p99"], 39.6)
        self.assertEqual(result["max"], 40)
        self.assertEqual(audit.stats([]), {"count": 0, "min": None, "p50": None,
                                         "p95": None, "p99": None, "max": None})

    def test_latest_past_includes_exact_time_and_never_future(self):
        frames = [{"frame_id": 1}, {"frame_id": 2}, {"frame_id": 3}]
        times = [10, 20, 40]
        self.assertIsNone(audit.latest_past(frames, times, 9))
        self.assertEqual(audit.latest_past(frames, times, 20)["frame_id"], 2)
        self.assertEqual(audit.latest_past(frames, times, 39)["frame_id"], 2)

    def test_candidate_preserves_provenance_and_marks_unready_whole_group(self):
        original = observation(1, [10, 20, 30], 35)
        following = observation(2, [40, 50, 60], 65)
        grid = [{"session_id": "demo", "time_ns": 45, "segment_id": "0", "source_row": 1,
            "source_observation_monotonic_ns": 35,
            "images": {f["camera"]: f for f in original["frames"]}}]
        result = audit.compare_grid([original, following], grid, "demo")
        selected = result["selection_provenance"][0]["selected_frames"]
        self.assertEqual(selected["head"]["source_observation_id"], 2)
        self.assertTrue(selected["head"]["original_group_not_ready_at_grid"])
        self.assertEqual(selected["left_wrist"]["source_observation_id"], 1)
        self.assertTrue(all(f["host_received_monotonic_ns"] <= 45 for f in selected.values()))
        self.assertFalse(result["training_export_created"])

    def test_grid_session_mismatch_and_future_original_are_rejected(self):
        obs = observation(1, [10, 20, 30], 35)
        row = {"session_id": "wrong", "time_ns": 45, "segment_id": "0", "source_row": 1,
            "source_observation_monotonic_ns": 35, "images": {f["camera"]: f for f in obs["frames"]}}
        with self.assertRaisesRegex(ValueError, "session"):
            audit.compare_grid([obs], [row], "demo")
        row.update(session_id="demo", time_ns=34)
        with self.assertRaisesRegex(ValueError, "future"):
            audit.compare_grid([obs], [row], "demo")

    def test_grid_cannot_invent_source_observation_or_availability(self):
        obs = observation(1, [10, 20, 30], 35)
        row = {"session_id": "demo", "time_ns": 45, "segment_id": "0", "source_row": 1,
            "source_observation_monotonic_ns": 35, "images": {f["camera"]: f for f in obs["frames"]}}
        for field, value, message in [("source_row", 99, "source_row"),
                                       ("source_observation_monotonic_ns", 34, "availability")]:
            with self.subTest(field=field):
                changed = deepcopy(row)
                changed[field] = value
                with self.assertRaisesRegex(ValueError, message):
                    audit.compare_grid([obs], [changed], "demo")

    def test_grid_frame_fields_must_match_audited_raw_source(self):
        obs = observation(1, [10, 20, 30], 35)
        row = {"session_id": "demo", "time_ns": 45, "segment_id": "0", "source_row": 1,
            "source_observation_monotonic_ns": 35, "images": {f["camera"]: f for f in obs["frames"]}}
        changes = {"frame_id": 99, "host_received_monotonic_ns": 11,
                   "timestamp": {"unit": "ns", "value": 999, "clock_domain": "unverified"},
                   "payload_sha256": "invented", "path": "wrong.pack", "payload_offset": 100,
                   "payload_bytes": 3}
        for field, value in changes.items():
            with self.subTest(field=field):
                changed = deepcopy(row)
                changed["images"]["head"][field] = value
                with self.assertRaisesRegex(ValueError, "frame provenance"):
                    audit.compare_grid([obs], [changed], "demo")

    def test_main_protects_external_episode_reached_by_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collection = root / "collection"
            collection.mkdir()
            source = root / "external-episode"
            (source / "journal").mkdir(parents=True)
            (source / "journal/manifest.json").write_text("{}")
            (collection / "episode-link").symlink_to(source, target_is_directory=True)
            output = source / "new-output.json"
            with self.assertRaisesRegex(ValueError, "inside source"):
                audit.main(["--raw-root", str(collection), "--output", str(output)])
            self.assertFalse(output.exists())

    def test_repeat_stamp_is_not_duplicate_bytes_and_tamper_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "head.pack").write_bytes(b"ab")
            a = observation(1, [10, 20, 30], 35)["frames"][0]
            b = dict(a, frame_id=2, host_received_monotonic_ns=40)
            a.update(payload_offset=0, payload_sha256=hashlib.sha256(b"a").hexdigest(),
                     source_observation_available_ns=35)
            b.update(payload_offset=1, payload_sha256=hashlib.sha256(b"b").hexdigest(),
                     source_observation_available_ns=65)
            result = audit.camera_metrics([a, b], root)
            self.assertEqual(result["source_stamp_repeat_count"], 1)
            self.assertEqual(result["repeated_stamp_pairs_with_different_verified_bytes"], 1)
            (root / "head.pack").write_bytes(b"ac")
            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                audit.camera_metrics([a, b], root)

    def test_source_output_overwrite_and_payload_escape_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            with self.assertRaisesRegex(ValueError, "inside source"):
                audit.output_path_check(source / "result.json", [source])
            existing = root / "old.json"
            existing.write_text("original")
            with self.assertRaises(FileExistsError):
                audit.output_path_check(existing, [source])
            with self.assertRaisesRegex(ValueError, "outside source"):
                audit.contained_file(source, "../old.json")
            self.assertEqual(existing.read_text(), "original")

    def test_clock_units_and_receipt_order_are_not_silently_reinterpreted(self):
        first = observation(1, [10, 20, 30], 35)
        second = observation(2, [10, 40, 50], 55)
        with self.assertRaisesRegex(ValueError, "non-increasing"):
            audit.frame_pools([first, second])
        first["frames"][0]["timestamp"]["unit"] = "us"
        with self.assertRaisesRegex(ValueError, "nanosecond"):
            audit.frame_pools([first])


if __name__ == "__main__":
    unittest.main()

"""Synthetic-only contract tests: no raw task data and no ROS dependency."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "scripts"))
sys.path.insert(0, str(TASK / "robot"))
from export_replay_bundle import export_bundle
from f1_replay.bundle import ARM_NAMES, BODY_NAMES, GRIPPER_NAMES, load_bundle


class ReplayBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "episode.npz"
        self.output = self.root / "bundle"
        self.arrays = {
            "time_ns": np.asarray([1000000000, 1033333333, 1075000000, 1090000000, 1130000000], dtype=np.int64),
            "state": np.arange(105, dtype=np.float32).reshape(5, 21) / 7,
            "action": np.arange(95, dtype=np.float32).reshape(5, 19) / 9 - 4,
            "state_valid": np.ones((5, 21), dtype=bool),
            "action_candidate_valid": np.ones((5, 19), dtype=bool),
            "future_signal_action_candidate_valid": np.ones(5, dtype=bool),
            "source_relative_path": np.asarray("batch/episode/data.mcap"),
        }
        self.meta = {"episode": "batch/episode", "source_relative_path": "batch/episode/data.mcap",
                     "state_dim": 21, "action_dim": 19, "frames": 5}
        self.write_source()

    def write_source(self):
        np.savez_compressed(self.source, **self.arrays)
        self.source.with_suffix(".json").write_text(json.dumps(self.meta), encoding="utf-8")

    def export(self, start=1, stop=4):
        return export_bundle(self.source, self.output, "synthetic_cleaning_v1", start, stop)

    def manifest(self):
        return json.loads((self.output / "manifest.json").read_text())

    def write_manifest(self, manifest):
        (self.output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def rewrite_frames(self, transform):
        rows = [json.loads(line) for line in (self.output / "frames.jsonl").read_text().splitlines()]
        transform(rows)
        payload = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
        (self.output / "frames.jsonl").write_bytes(payload)
        manifest = self.manifest()
        manifest["frames"].update(size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        self.write_manifest(manifest)

    def test_all_19_actions_and_21_states_preserve_values_and_nonuniform_source_time(self):
        bundle = self.export()
        self.assertEqual(bundle.manifest["status"], "candidate")
        self.assertEqual(bundle.manifest["layout"]["state"]["names"], list(ARM_NAMES + GRIPPER_NAMES + BODY_NAMES))
        for index, frame in enumerate(bundle.frames, 1):
            self.assertEqual(frame.action, tuple(float(v) for v in self.arrays["action"][index]))
            self.assertEqual(frame.state, tuple(float(v) for v in self.arrays["state"][index]))
            self.assertEqual(frame.source_time_ns, int(self.arrays["time_ns"][index]))
            self.assertEqual(frame.time_ns, int(self.arrays["time_ns"][index] - self.arrays["time_ns"][1]))
        self.assertEqual(bundle.duration_ns, 96666667)
        self.assertEqual(bundle.manifest["timing"]["final_hold_ns"], 40000000)
        self.assertEqual(bundle.sha256, hashlib.sha256((self.output / "manifest.json").read_bytes()).hexdigest())
        self.assertEqual(bundle.manifest["source"]["aligned_npz"]["sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(load_bundle(self.output / "manifest.json").path, self.output.resolve())

    def test_one_frame_uses_its_actual_successor_as_end_and_never_extrapolates(self):
        bundle = self.export(2, 3)
        self.assertEqual(len(bundle.frames), 1)
        self.assertEqual(bundle.frames[0].time_ns, 0)
        self.assertEqual(bundle.duration_ns, 15000000)

    def test_unknown_tail_and_invalid_index_ranges_are_rejected(self):
        for start, stop in ((0, 5), (2, 2), (-1, 3), (4, 3), (True, 3)):
            with self.subTest(start=start, stop=stop), self.assertRaises(ValueError):
                self.export(start, stop)
        self.assertFalse(self.output.exists())

    def test_invalid_masks_do_not_drop_frames_or_compress_gaps(self):
        for key, index in (("state_valid", (2, 7)), ("action_candidate_valid", (2, 18)),
                           ("future_signal_action_candidate_valid", 2)):
            with self.subTest(mask=key):
                self.arrays[key][index] = False
                self.write_source()
                with self.assertRaisesRegex(ValueError, "Invalid"):
                    self.export()
                self.assertFalse(self.output.exists())
                self.arrays[key][index] = True

    def test_nonfinite_selected_values_rejected_but_unselected_invalid_boundary_is_not_replayed(self):
        self.arrays["action"][2, 16] = np.nan
        self.write_source()
        with self.assertRaisesRegex(ValueError, "non-finite"):
            self.export()
        self.arrays["action"][2, 16] = 0
        self.arrays["action"][4] = np.nan
        self.arrays["action_candidate_valid"][4] = False
        self.write_source()
        self.assertEqual(len(self.export().frames), 3)

    def test_source_duplicate_backward_and_float_timestamps_rejected(self):
        original = self.arrays["time_ns"].copy()
        for times in (np.asarray([1, 2, 2, 4, 5]), np.asarray([1, 3, 2, 4, 5]), original.astype(float)):
            self.arrays["time_ns"] = times
            self.write_source()
            with self.assertRaisesRegex(ValueError, "time_ns"):
                self.export()

    def test_source_metadata_identity_shape_and_pickle_rejected(self):
        self.meta["source_relative_path"] = "wrong.mcap"
        self.write_source()
        with self.assertRaisesRegex(ValueError, "identities"):
            self.export()
        self.meta["source_relative_path"] = "batch/episode/data.mcap"
        self.arrays["action"] = np.zeros((5, 18), dtype=np.float32)
        self.write_source()
        with self.assertRaisesRegex(ValueError, "action must"):
            self.export()
        self.arrays["action"] = np.zeros((5, 19), dtype=object)
        self.write_source()
        with self.assertRaises(ValueError):
            self.export()

    def test_loader_rejects_swapped_names_wrong_units_or_unrecognized_layout(self):
        self.export()
        original = self.manifest()
        for change in ("order", "units", "extra"):
            modified = json.loads(json.dumps(original))
            if change == "order":
                names = modified["layout"]["action"]["names"]
                names[0], names[1] = names[1], names[0]
            elif change == "units":
                modified["layout"]["action"]["units"][0] = "rad"
            else:
                modified["layout"]["body_control"] = True
            self.write_manifest(modified)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "layout"):
                load_bundle(self.output)

    def test_loader_rejects_hash_and_size_mismatches(self):
        self.export()
        original = self.manifest()
        modified = json.loads(json.dumps(original))
        modified["frames"]["sha256"] = "0" * 64
        self.write_manifest(modified)
        with self.assertRaisesRegex(ValueError, "SHA256"):
            load_bundle(self.output)
        modified = json.loads(json.dumps(original))
        modified["frames"]["size_bytes"] += 1
        self.write_manifest(modified)
        with self.assertRaisesRegex(ValueError, "size"):
            load_bundle(self.output)

    def test_loader_rejects_nonfinite_boolean_and_noninteger_time_even_with_valid_hash(self):
        self.export()
        original_frames = (self.output / "frames.jsonl").read_bytes()
        original_manifest = self.manifest()
        for field, value in (("time_ns", 0.5), ("source_time_ns", True), ("action", float("nan")),
                             ("action", float("inf")), ("state", True)):
            (self.output / "frames.jsonl").write_bytes(original_frames)
            self.write_manifest(original_manifest)
            def transform(rows):
                if field in ("action", "state"):
                    rows[0][field][0] = value
                else:
                    rows[0][field] = value
            self.rewrite_frames(transform)
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                load_bundle(self.output)

    def test_loader_rejects_duplicate_time_and_extended_final_hold(self):
        self.export()
        self.rewrite_frames(lambda rows: rows[1].update(time_ns=0, source_time_ns=rows[0]["source_time_ns"]))
        with self.assertRaisesRegex(ValueError, "increasing"):
            load_bundle(self.output)
        manifest = self.manifest()
        manifest["duration_ns"] += 1
        self.write_manifest(manifest)
        with self.assertRaisesRegex(ValueError, "boundary"):
            load_bundle(self.output)

    def test_loader_rejects_path_escape_and_external_symlink(self):
        self.export()
        manifest = self.manifest()
        manifest["frames"]["path"] = "../frames.jsonl"
        self.write_manifest(manifest)
        with self.assertRaisesRegex(ValueError, "inside"):
            load_bundle(self.output)
        manifest["frames"]["path"] = "frames.jsonl"
        self.write_manifest(manifest)
        original = self.output / "frames.jsonl"
        external = self.root / "outside.jsonl"
        original.rename(external)
        original.symlink_to(external)
        with self.assertRaisesRegex(ValueError, "inside"):
            load_bundle(self.output)

    def test_existing_output_never_overwritten_and_loader_needs_no_site_packages(self):
        bundle = self.export()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.export()
        self.assertEqual(load_bundle(self.output).sha256, bundle.sha256)
        code = "import sys; sys.path.insert(0, sys.argv[1]); from f1_replay.bundle import load_bundle; b=load_bundle(sys.argv[2]); assert len(b.frames)==3; assert 'numpy' not in sys.modules"
        result = subprocess.run([sys.executable, "-S", "-c", code, str(TASK / "robot"), str(self.output)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_candidate_and_replay_ready_status_are_readable_but_unknown_status_is_rejected(self):
        self.export()
        manifest = self.manifest()
        manifest["status"] = "replay_ready"
        self.write_manifest(manifest)
        self.assertEqual(load_bundle(self.output).manifest["status"], "replay_ready")
        manifest["status"] = "approved_by_shape"
        self.write_manifest(manifest)
        with self.assertRaisesRegex(ValueError, "status"):
            load_bundle(self.output)

    def test_failed_final_validation_never_leaves_a_completed_manifest(self):
        with patch("export_replay_bundle.load_bundle", side_effect=ValueError("simulated validation failure")):
            with self.assertRaisesRegex(ValueError, "simulated"):
                self.export()
        self.assertFalse((self.output / "manifest.json").exists())
        self.assertFalse((self.output / "manifest.json.tmp").exists())

    def test_duplicate_json_keys_are_rejected_before_execution(self):
        self.export()
        path = self.output / "manifest.json"
        content = path.read_text()
        path.write_text(content.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'))
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            load_bundle(self.output)


if __name__ == "__main__":
    unittest.main()

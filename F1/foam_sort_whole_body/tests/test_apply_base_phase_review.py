"""Bind repair candidates to actual parent identities without relaxing masks."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import apply_base_phase_review as apply_review
import base_relabel


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="f1-base-import-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "repair_v1"
        self.origin = 1_780_000_000_123_456_789
        self.episode = "batch/episode_000057"
        self.relative = self.episode + "/episode_000057_0.mcap"
        raw = self.root / "source.mcap"
        raw.write_bytes(b"fixed synthetic raw identity")
        signal, camera = self.root / "signal.npz", self.root / "camera.npz"
        np.savez(signal, hal_t_ns=np.array([self.origin], dtype=np.int64))
        np.savez(camera, head_log_ns=np.array([self.origin], dtype=np.int64))
        original, masked = self.root / "original.npz", self.root / "masked.npz"
        arrays = {"time_ns": self.origin + np.array([0, 100_000_000, 200_000_000, 300_000_000], dtype=np.int64),
                  "state": np.ones((4, 21), dtype=np.float32), "action": np.ones((4, 19), dtype=np.float32),
                  "action_candidate_valid": np.ones((4, 19), dtype=bool),
                  "joint_frame_candidate_valid": np.ones(4, dtype=bool),
                  "source_relative_path": np.asarray(self.relative)}
        np.savez(original, **arrays)
        arrays["action_candidate_valid"][3:] = False
        arrays["joint_frame_candidate_valid"][3:] = False
        np.savez(masked, **arrays)
        original_meta = {"episode": self.episode, "source_relative_path": self.relative,
                         "start_ns": self.origin, "end_ns": self.origin + 1_000_000_000 - 1,
                         "frames": 4, "split": "train", "camera_npz_sha256": self.sha(camera)}
        self.write(original.with_suffix(".json"), original_meta)
        parent_meta = {**original_meta, "parent_aligned_npz_sha256": self.sha(original),
                       "parent_aligned_metadata_sha256": self.sha(original.with_suffix(".json")),
                       "tail_mask_start_index": 3, "tail_mask_policy": "all_19D_invalid_from_boundary"}
        self.write(masked.with_suffix(".json"), parent_meta)
        self.source = {"episode": self.episode, "source_start_ns": self.origin,
                       "source_duration_ns": 1_000_000_000, "source_stop_ns_exclusive": self.origin + 1_000_000_000,
                       "frames": 4, "parent_split": "train", "source_relative_path": self.relative,
                       "raw_path": str(raw), "source_fingerprint": {"size_bytes": raw.stat().st_size, "mtime_ns": raw.stat().st_mtime_ns},
                       "aligned_npz_path": str(original), "aligned_npz_sha256": self.sha(original),
                       "aligned_metadata_sha256": self.sha(original.with_suffix(".json")),
                       "signal_npz_path": str(signal), "signal_npz_sha256": self.sha(signal),
                       "camera_npz_path": str(camera), "camera_npz_sha256": self.sha(camera)}
        self.mask = {"episode": self.episode, "parent_split": "train", "aligned_npz_path": str(masked),
                     "aligned_npz_sha256": self.sha(masked), "aligned_metadata_sha256": self.sha(masked.with_suffix(".json")),
                     "parent_aligned_npz_sha256": self.sha(original), "tail_mask_start_index": 3, "tail_mask_start_s": .3}
        self.registries = {key: self.root / f"{key}.jsonl" for key in apply_review.REGISTRIES}
        self.save_registries()
        self.review = self.root / "review.json"
        self.phase = {"schema": base_relabel.PHASE_SCHEMA, "episode": self.episode,
                      "source_origin_ns": str(self.origin), "source_duration_s": 1.0,
                      "coordinate_convention": base_relabel.COORDINATES, "segments": []}
        self.write(self.review, self.phase)

    def sha(self, path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def write(self, path, value):
        Path(path).write_text(json.dumps(value))

    def save_registries(self, current=True):
        self.write(self.registries["source_775"], self.source)
        self.write(self.registries["masks_775"], self.mask)
        self.registries["current_772"].write_text(json.dumps(self.source) if current else "")

    def run_import(self):
        return apply_review.run(self.review, self.output, self.registries)

    def test_empty_real_bound_request_does_not_reuse_old_actions_or_relax_tail(self):
        originals = {path: path.read_bytes() for path in self.root.iterdir() if path.is_file()}
        result = self.run_import()
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["candidate_counts"]["candidate_present"], 0)
        self.assertTrue(result["source_eligibility"]["in_current_candidate"])
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)
        with np.load(self.output / "candidate/base_relabel.npz") as candidate, np.load(self.mask["aligned_npz_path"]) as parent:
            np.testing.assert_array_equal(candidate["time_ns"], parent["time_ns"])
            self.assertTrue(np.isnan(candidate["base_candidate_v"]).all())
            self.assertFalse(candidate["base_candidate_present"].any())
            self.assertNotIn("action", candidate.files)
        report = json.loads((self.output / "import_report.json").read_text())
        self.assertNotIn("training_approved", report)
        self.assertFalse(report["full_19d_pack_generated"])
        self.assertFalse(report["original_selection_and_masks_modified"])

    def test_excluded_diagnostic_source_stays_excluded(self):
        self.save_registries(current=False)
        result = self.run_import()
        self.assertFalse(result["source_eligibility"]["in_current_candidate"])
        self.assertEqual(result["source_eligibility"]["status"], "excluded_diagnostic_source_not_restored")
        self.assertEqual(self.registries["current_772"].read_text(), "")

    def test_wrong_episode_origin_duration_and_declared_provenance_are_rejected(self):
        changes = [{"episode": "batch/other"}, {"source_origin_ns": str(self.origin + 1)},
                   {"source_duration_s": 1.000000001}, {"source_duration_s": 1.0000000001},
                   {"provenance": {"signal_npz_sha256": "bad"}}, {"provenance": {"camera_npz_sha256": "bad"}}]
        for change in changes:
            with self.subTest(change=change):
                self.write(self.review, {**self.phase, **change})
                with self.assertRaises(base_relabel.RelabelError):
                    self.run_import()
                self.assertFalse(self.output.exists())

    def test_all_derived_source_product_hashes_are_verified(self):
        paths = [self.source["aligned_npz_path"], Path(self.source["aligned_npz_path"]).with_suffix(".json"),
                 self.mask["aligned_npz_path"], Path(self.mask["aligned_npz_path"]).with_suffix(".json"),
                 self.source["signal_npz_path"], self.source["camera_npz_path"]]
        for path in map(Path, paths):
            before = path.read_bytes()
            try:
                path.write_bytes(before + b"changed")
                with self.subTest(path=path), self.assertRaisesRegex(base_relabel.RelabelError, "SHA256"):
                    self.run_import()
            finally:
                path.write_bytes(before)

    def test_current_selection_cannot_point_to_different_source_or_split(self):
        self.write(self.registries["current_772"], {**self.source, "parent_split": "val"})
        with self.assertRaisesRegex(base_relabel.RelabelError, "different source identity"):
            self.run_import()

    def test_raw_fingerprint_change_is_detected(self):
        Path(self.source["raw_path"]).write_bytes(b"a different recording")
        with self.assertRaisesRegex(base_relabel.RelabelError, "raw MCAP"):
            self.run_import()

    def test_self_consistent_manifest_with_relaxed_tail_is_rejected(self):
        path = Path(self.mask["aligned_npz_path"])
        with np.load(path) as data:
            arrays = {key: data[key] for key in data.files}
        arrays["action_candidate_valid"][3, 0] = True
        np.savez(path, **arrays)
        self.mask["aligned_npz_sha256"] = self.sha(path)
        self.save_registries()
        with self.assertRaisesRegex(base_relabel.RelabelError, "all-19D invalid"):
            self.run_import()

    def test_verified_reuse_and_changed_review_refuses_overwrite(self):
        self.run_import()
        product_before = {str(path.relative_to(self.output)): path.read_bytes()
                          for path in self.output.rglob("*") if path.is_file()}
        self.assertEqual(self.run_import()["status"], "reused")
        self.write(self.review, {**self.phase, "notes": "new review content"})
        with self.assertRaisesRegex(base_relabel.RelabelError, "new output version"):
            self.run_import()
        self.assertEqual(product_before, {str(path.relative_to(self.output)): path.read_bytes()
                                        for path in self.output.rglob("*") if path.is_file()})

    def test_generation_failure_does_not_publish_and_retry_succeeds(self):
        with mock.patch.object(apply_review.base_relabel, "run", side_effect=OSError("simulated failure")):
            with self.assertRaisesRegex(OSError, "simulated failure"):
                self.run_import()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".repair_v1.partial-*")), [])
        self.assertEqual(self.run_import()["status"], "created")

    def test_tampered_import_artifact_cannot_be_reused(self):
        self.run_import()
        path = self.output / "repair_binding.json"
        path.write_text(path.read_text() + " ")
        with self.assertRaisesRegex(base_relabel.RelabelError, "artifact.*changed"):
            self.run_import()


if __name__ == "__main__":
    unittest.main()

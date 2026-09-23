"""Semantic slicing regressions; synthetic evidence does not approve real data."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

SPEC = importlib.util.spec_from_file_location("cycle_review", Path(__file__).resolve().parents[1] / "scripts/prepare_cycle_review.py")
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


class CycleReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.row = {"episode": "batch/episode_000001", "source_duration_ns": 6_000_000_000,
                    "parent_split": "validation", "confirmed_bad_base_masked": False,
                    "source_start_ns": 10_000_000_000, "chunk_horizon": 50,
                    "rough_intervals_s_hint": [], "aligned_npz_sha256": "fixture"}
        self.annotation = review.make_annotation(self.row, {})
        self.cycle = review.template_cycle(1, (0, 6_000_000_000))
        self.annotation["cycles"] = [self.cycle]
        grid = self.row["source_start_ns"] + np.rint(np.arange(180) * 1e9 / 30).astype(np.int64)
        starts = np.zeros(180, dtype=bool)
        starts[:131] = True
        self.arrays = {"time_ns": grid, "future_signal_action_candidate_valid": np.ones(180, dtype=bool),
                       "camera_timestamp_candidate_valid": np.ones((180, 3), dtype=bool),
                       "chunk_start_candidate_valid": starts}
        self.times = {"signal": grid.copy(), "camera": grid.copy()}

    def tearDown(self):
        self.temp.cleanup()

    def approve_fixture(self):
        path = self.root / "review.txt"
        path.write_text("Synthetic evidence for unit tests only; no actual source is approved.\n")
        evidence = []
        for name, kind in (("visual", "continuous_three_camera_review"), ("mapping", "interface_mapping_review"),
                           ("clock", "clock_alignment_review")):
            evidence.append({"evidence_id": name, "kind": kind, "path": path.name, "sha256": review.digest(path),
                             "reviewer": "unit_test_fixture", "reviewed_at": "2026-09-14T00:00:00+00:00",
                             "episode": self.row["episode"], "interval_ns": [0, 6_000_000_000],
                             "cameras": ["head", "left", "right"]})
        self.annotation["evidence"] = evidence
        self.cycle.update(status="reviewed", task_outcome="success", human_intervention="none",
                          base_label_status="reviewed_original_weak")
        for gate, value in self.cycle["reviews"].items():
            value.update(status="verified", evidence_refs=["mapping" if gate == "interface" else "clock" if gate == "time" else "visual"])

    def test_pending_stores_but_cannot_export(self):
        self.assertEqual(review.validate_annotation(self.annotation, self.row, self.root), [])
        with self.assertRaisesRegex(ValueError, "Pending/rejected"):
            review.reviewed_cycle(self.cycle, self.row, [], {})

    def test_child_split_and_source_identity_are_frozen(self):
        self.annotation["parent_split"] = "train"
        with self.assertRaisesRegex(ValueError, "inherit frozen"):
            review.validate_annotation(self.annotation, self.row, self.root)
        self.annotation["parent_split"] = "validation"
        self.annotation["catalog_entry_sha256"] = "changed"
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            review.validate_annotation(self.annotation, self.row, self.root)

    def test_cycle_overlap_and_out_of_bounds_are_rejected(self):
        second = review.template_cycle(2, (3_000_000_000, 6_000_000_000))
        self.annotation["cycles"].append(second)
        with self.assertRaisesRegex(ValueError, "overlapping"):
            review.validate_annotation(self.annotation, self.row, self.root)
        self.annotation["cycles"] = [self.cycle]
        self.cycle["interval_ns"] = [0, 6_000_000_001]
        with self.assertRaisesRegex(ValueError, "outside source"):
            review.validate_annotation(self.annotation, self.row, self.root)

    def test_known_bad_base_cannot_be_cleared_by_review_flags(self):
        self.approve_fixture()
        self.row["confirmed_bad_base_masked"] = True
        self.annotation["catalog_entry_sha256"] = review.object_digest(self.row)
        with self.assertRaisesRegex(ValueError, "Known bad base status cannot be cleared"):
            review.validate_annotation(self.annotation, self.row, self.root)
        self.cycle["base_label_status"] = "known_bad"
        with self.assertRaisesRegex(ValueError, "Known bad base cannot be restored"):
            review.validate_annotation(self.annotation, self.row, self.root)

    def test_sparse_evidence_or_altered_evidence_is_not_approval(self):
        self.approve_fixture()
        self.annotation["evidence"][0]["kind"] = "sparse_visual_hint"
        with self.assertRaisesRegex(ValueError, "continuous three-camera"):
            review.validate_annotation(self.annotation, self.row, self.root)
        self.annotation["evidence"][0]["kind"] = "continuous_three_camera_review"
        (self.root / "review.txt").write_text("changed evidence")
        with self.assertRaisesRegex(ValueError, "checksum changed"):
            review.validate_annotation(self.annotation, self.row, self.root)

    def test_complete_cycle_review_inherits_split_and_requires_all_gates(self):
        self.approve_fixture()
        accepted = review.validate_annotation(self.annotation, self.row, self.root)
        slices = review.cycle_slices(accepted[0], self.row, self.annotation, self.arrays, self.times)
        self.assertEqual(slices[0]["parent_split"], "validation")
        self.assertEqual(slices[0]["chunk_start_source_frames"], list(range(131)))
        self.assertFalse(slices[0]["training_approved"])
        self.cycle["reviews"]["interface"]["status"] = "pending"
        with self.assertRaisesRegex(ValueError, "interface: review not verified"):
            review.validate_annotation(self.annotation, self.row, self.root)

    def test_half_open_cycles_do_not_cross_cut_or_pad_tail(self):
        self.approve_fixture()
        self.cycle["interval_ns"] = [0, 3_000_000_000]
        first = review.cycle_slices(self.cycle, self.row, self.annotation, self.arrays, self.times)[0]
        second_cycle = deepcopy(self.cycle)
        second_cycle.update(cycle_id="cycle_002", interval_ns=[3_000_000_000, 6_000_000_000])
        second = review.cycle_slices(second_cycle, self.row, self.annotation, self.arrays, self.times)[0]
        self.assertEqual(first["chunk_start_source_frames"], list(range(41)))
        self.assertEqual(second["chunk_start_source_frames"], list(range(90, 131)))
        self.assertLess(first["chunk_start_source_frames"][-1] + 49, 90)
        self.assertLess(second["chunk_start_source_frames"][-1] + 49, 180)

    def test_subframe_human_interval_still_splits_and_old_observation_is_rejected(self):
        self.approve_fixture()
        # There is no 30Hz sample inside this one-millisecond intervention.
        self.annotation["exclusions"] = [{"interval_ns": [3_001_000_000, 3_002_000_000], "status": "reviewed"}]
        self.times["camera"][91] = self.arrays["time_ns"][90]
        slices = review.cycle_slices(self.cycle, self.row, self.annotation, self.arrays, self.times)
        self.assertEqual(len(slices), 2)
        self.assertEqual(slices[0]["chunk_start_source_frames"][-1], 41)
        self.assertEqual(slices[1]["chunk_start_source_frames"][0], 92)
        self.assertTrue(all(i + 49 < 91 for i in slices[0]["chunk_start_source_frames"]))

    def test_future_camera_failure_is_allowed_but_signal_gap_is_not(self):
        self.approve_fixture()
        self.arrays["camera_timestamp_candidate_valid"][25] = False
        self.arrays["future_signal_action_candidate_valid"][100] = False
        slices = review.cycle_slices(self.cycle, self.row, self.annotation, self.arrays, self.times)
        starts = slices[0]["chunk_start_source_frames"]
        self.assertIn(0, starts)  # Future image is not an input to this action chunk.
        self.assertNotIn(25, starts)
        self.assertFalse(any(i <= 100 < i + 50 for i in starts))


if __name__ == "__main__":
    unittest.main()

"""Semantic checks for explicit weak chassis labels and immutable version reuse."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import base_relabel as relabel


ORIGIN = 1_789_123_456_123_456_789


def phase(segments=None, duration=10.0):
    return {"schema": relabel.PHASE_SCHEMA, "episode": "test_batch/episode_000007",
            "source_origin_ns": str(ORIGIN), "source_duration_s": duration,
            "coordinate_convention": relabel.COORDINATES, "segments": segments or []}


def motion(sid="out-back", start=1.0, end=3.0, delta=(-0.5, 0.0, 0.0),
           timing="reviewed", cycle="cycle-1"):
    return {"id": sid, "cycle_id": cycle, "kind": "motion", "start_s": start, "end_s": end,
            "dx_m": delta[0], "dy_m": delta[1], "dyaw_rad": delta[2],
            "timing_status": timing, "distance_provenance": {"kind": "task_prior", "source": "test distance"},
            "evidence": [{"video": "source.mp4", "criterion": "fixed background transition"}],
            "reviewer": "test reviewer"}


def stationary(sid="stop", start=3.0, end=4.0):
    result = motion(sid, start, end, (0.0, 0.0, 0.0))
    result.update(kind="stationary", distance_provenance="explicit observed stationary interval",
                  evidence=[{"video": "source.mp4", "criterion": "stable background, head and body"}])
    return result


def times(values):
    return np.array([ORIGIN + round(float(value) * 1_000_000_000) for value in values], dtype=np.int64)


class NumericCandidateTests(unittest.TestCase):
    def test_uncovered_time_is_nan_and_final_endpoint_is_not_repeated(self):
        record = phase([motion(), stationary(), motion("return", 6, 10, (0.5, 0, 0))])
        arrays, report = relabel.build_candidate(record, times([0, 1, 2, 3, 4, 5, 6, 7, 9, 10]))
        self.assertEqual(arrays["base_candidate_present"].tolist(),
                         [False, True, True, True, False, False, True, True, True, False])
        self.assertTrue(np.isnan(arrays["base_candidate_v"][[0, 4, 5, 9]]).all())
        self.assertEqual(arrays["segment_index"].tolist(), [-1, 0, 0, 1, -1, -1, 2, 2, 2, -1])
        self.assertTrue(np.all(arrays["base_candidate_v"][3] == 0))
        self.assertEqual(report["unknown_intervals"], [{"start_s": 0.0, "end_s": 1.0},
                                                     {"start_s": 4.0, "end_s": 6.0}])
        self.assertEqual(report["time_axis"]["endpoint_samples"], 1)
        self.assertFalse(report["training_approved"])

    def test_displacement_integral_and_peak_preserve_both_axes_and_yaw(self):
        delta = (0.7, -1.6, 0.3)
        arrays, report = relabel.build_candidate(phase([motion(delta=delta)]), times(np.arange(0, 10, .001)))
        selection = arrays["segment_index"] == 0
        dt = np.diff(arrays["time_ns"][selection]).astype(np.float64) / 1e9
        values = arrays["base_candidate_v"][selection]
        integral = np.sum((values[1:] + values[:-1]) * dt[:, None] / 2, axis=0)
        np.testing.assert_allclose(integral, delta, rtol=2e-6, atol=1e-9)
        np.testing.assert_allclose(np.max(np.abs(values), axis=0), np.abs(delta) * .75)
        np.testing.assert_allclose(report["segments"][0]["analytic_peak_abs_ax_ay_alpha"],
                                   np.abs(delta) * 1.5)
        self.assertEqual(arrays["base_candidate_v"].shape, (10000, 3))
        self.assertFalse(report["training_approved"])

    def test_multicycle_and_arbitrary_segments_do_not_force_six_parts(self):
        rows = [motion("c2-side", 8, 9, (0, 1.6, 0), cycle=2),
                motion("c1-out", 1, 2, (-.5, -.6, 0), cycle=1), stationary("pause", 2, 2.4),
                motion("c1-return", 4, 5, (0, .6, 0), cycle=1)]
        arrays, report = relabel.build_candidate(phase(rows), times([1.5, 2.2, 3, 4.5, 8.5]))
        self.assertEqual(arrays["segment_index"].tolist(), [1, 2, -1, 3, 0])
        self.assertEqual(report["counts"]["motion_segments"], 3)
        self.assertEqual([row["cycle_id"] for row in report["segments"]], [2, 1, "cycle-1", 1])

    def test_short_window_is_not_extended_and_exposes_high_acceleration(self):
        row = motion(start=9.8, end=10.0)
        arrays, report = relabel.build_candidate(phase([row]), times([9.7, 9.8, 9.9, 9.95, 10]))
        self.assertEqual(arrays["base_candidate_present"].tolist(), [False, True, True, True, False])
        self.assertEqual(report["segments"][0]["duration_s"], .2)
        self.assertAlmostEqual(report["segments"][0]["analytic_peak_abs_vx_vy_wz"][0], 3.75)
        self.assertAlmostEqual(report["segments"][0]["analytic_peak_abs_ax_ay_alpha"][0], 75)

    def test_proposed_labels_are_not_reviewed_and_unknown_does_not_become_zero(self):
        row = motion(timing="proposed")
        row["evidence"] = []
        row.pop("reviewer")
        arrays, report = relabel.build_candidate(phase([row]), times([0, 1, 2, 3]))
        self.assertTrue(arrays["base_candidate_present"][2])
        self.assertFalse(arrays["base_timing_reviewed"].any())
        self.assertTrue(np.isnan(arrays["base_candidate_v"][0]).all())
        self.assertIn("timing_not_reviewed", {item["code"] for item in report["unresolved_reasons"]})

    def test_empty_phase_list_is_a_valid_all_unknown_candidate(self):
        arrays, report = relabel.build_candidate(phase(), times([0, 1, 9]))
        self.assertTrue(np.isnan(arrays["base_candidate_v"]).all())
        self.assertFalse(arrays["base_candidate_present"].any())
        self.assertFalse(arrays["base_timing_reviewed"].any())
        self.assertTrue((arrays["segment_index"] == -1).all())
        self.assertEqual(report["unknown_intervals"], [{"start_s": 0.0, "end_s": 10.0}])
        self.assertIn("no_segments", {item["code"] for item in report["unresolved_reasons"]})

    def test_boundary_nanosecond_precision_keeps_absolute_epoch_as_int64(self):
        row = motion(start=.033333333, end=.066666667)
        grid = np.array([ORIGIN + 33_333_332, ORIGIN + 33_333_333,
                         ORIGIN + 66_666_666, ORIGIN + 66_666_667], dtype=np.int64)
        arrays, _ = relabel.build_candidate(phase([row]), grid)
        np.testing.assert_array_equal(arrays["time_ns"], grid)
        self.assertEqual(arrays["base_candidate_present"].tolist(), [False, True, True, False])

    def test_unsampled_motion_remains_reported_instead_of_becoming_a_valid_zero(self):
        row = motion(start=1.001, end=1.009)
        arrays, report = relabel.build_candidate(phase([row]), times([1, 1.033333333]))
        self.assertFalse(arrays["base_candidate_present"].any())
        self.assertEqual(report["segments"][0]["sample_count"], 0)
        self.assertEqual(report["segments"][0]["boundary_augmented_trapezoid_integral_dx_dy_dyaw"], [0, 0, 0])
        self.assertIn("motion_segment_undersampled", {r["code"] for r in report["unresolved_reasons"]})

    def test_illegal_boundaries_displacements_and_review_claims_are_rejected(self):
        mutations = [
            {"start_s": -1}, {"start_s": 3}, {"end_s": 11}, {"end_s": float("nan")},
            {"end_s": float("inf")}, {"start_s": 1, "end_s": 1.0000000001},
            {"dx_m": float("inf")}, {"dx_m": True}, {"dx_m": 0},
            {"kind": "stationary"}, {"timing_status": "accepted"}, {"evidence": []},
            {"reviewer": " "}, {"distance_provenance": {}}, {"cycle_id": None},
        ]
        for change in mutations:
            with self.subTest(change=change):
                row = motion()
                row.update(change)
                with self.assertRaises(relabel.RelabelError):
                    relabel.build_candidate(phase([row]), times([0, 2, 9]))
        row = stationary()
        row.update(timing_status="proposed", evidence=[])
        with self.assertRaisesRegex(relabel.RelabelError, "evidence"):
            relabel.build_candidate(phase([row]), times([0, 2, 9]))

    def test_overlap_and_duplicate_id_are_rejected_but_shared_boundary_is_valid(self):
        for rows in ([motion(), motion("b", 2.9, 4)], [motion(), motion("b", 2.9999999999, 4)],
                     [motion(), motion("out-back", 4, 5)]):
            with self.subTest(rows=rows), self.assertRaises(relabel.RelabelError):
                relabel.build_candidate(phase(rows), times([0, 2, 9]))
        arrays, _ = relabel.build_candidate(phase([motion(), motion("b", 3, 4)]), times([3]))
        self.assertEqual(arrays["segment_index"].tolist(), [1])

    def test_timestamp_type_range_and_order_are_strict(self):
        bad_times = [np.array([0.0]), np.array([ORIGIN], dtype=np.uint64), np.array([], dtype=np.int64),
                     times([1, 1]), times([2, 1]), times([-1, 2]), times([2, 11]), times([2]).reshape((1, 1))]
        for grid in bad_times:
            with self.subTest(grid=grid), self.assertRaises(relabel.RelabelError):
                relabel.build_candidate(phase(), grid)
        for update in ({"source_origin_ns": ORIGIN}, {"coordinate_convention": "image pixels"},
                       {"source_duration_s": 0}, {"schema": "legacy_windows"}):
            data = phase()
            data.update(update)
            with self.subTest(update=update), self.assertRaises(relabel.RelabelError):
                relabel.build_candidate(data, times([0]))

    def test_explicit_top_level_reviewer_is_allowed_without_granting_training_acceptance(self):
        row = motion()
        row.pop("reviewer")
        data = phase([row])
        data["reviewer"] = "human reviewer on original frames"
        arrays, report = relabel.build_candidate(data, times([2]))
        self.assertTrue(arrays["base_timing_reviewed"][0])
        self.assertFalse(report["training_approved"])


class ProductIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="f1-base-relabel-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.phase_path = self.root / "phases.json"
        self.npz_path = self.root / "original.npz"
        self.output = self.root / "candidate_v1"
        self.phase_path.write_text(json.dumps(phase([motion(), stationary()])), encoding="utf-8")
        grid = times(np.arange(0, 10, 1/30))
        np.savez_compressed(self.npz_path, time_ns=grid, alternate_ns=grid, preserved_action=np.ones((len(grid), 19)))

    def run_product(self, **kwargs):
        return relabel.run(self.phase_path, self.npz_path, self.output, **kwargs)

    def test_exact_identity_reuse_preserves_inputs_products_and_acceptance_status(self):
        input_before = self.npz_path.read_bytes()
        first = self.run_product()
        original_products = {path.name: path.read_bytes() for path in self.output.iterdir()}
        second = self.run_product()
        self.assertEqual(first["status"], "created")
        self.assertEqual(second["status"], "reused")
        self.assertEqual(first["identity_sha256"], second["identity_sha256"])
        self.assertEqual(input_before, self.npz_path.read_bytes())
        self.assertEqual(original_products, {path.name: path.read_bytes() for path in self.output.iterdir()})
        manifest = json.loads((self.output / relabel.MANIFEST_NAME).read_text())
        for name, expected in manifest["artifacts"].items():
            self.assertEqual(expected["sha256"], hashlib.sha256((self.output / name).read_bytes()).hexdigest())
        with np.load(self.output / relabel.NPZ_NAME, allow_pickle=False) as result:
            self.assertEqual(set(result.files), {"time_ns", "base_candidate_v", "base_candidate_present",
                                                 "base_timing_reviewed", "segment_index"})
        self.assertFalse(manifest["training_approved"])

    def test_same_episode_path_and_segment_count_with_changed_timing_cannot_reuse(self):
        self.run_product()
        previous = (self.output / relabel.MANIFEST_NAME).read_bytes()
        data = json.loads(self.phase_path.read_text())
        data["segments"][0]["start_s"] = 1.1
        self.phase_path.write_text(json.dumps(data))
        with self.assertRaisesRegex(relabel.RelabelError, "new output version"):
            self.run_product()
        self.assertEqual(previous, (self.output / relabel.MANIFEST_NAME).read_bytes())

    def test_same_npz_path_and_frame_count_with_changed_source_cannot_reuse(self):
        self.run_product()
        with np.load(self.npz_path) as payload:
            values = {key: payload[key] for key in payload.files}
        values["preserved_action"][0, 0] = 4
        np.savez_compressed(self.npz_path, **values)
        with self.assertRaisesRegex(relabel.RelabelError, "new output version"):
            self.run_product()

    def test_parameter_change_cannot_reuse_even_when_selected_times_are_equal(self):
        self.run_product()
        with self.assertRaisesRegex(relabel.RelabelError, "new output version"):
            self.run_product(time_key="alternate_ns")

    def test_changed_script_is_part_of_identity(self):
        copied = self.root / "engine.py"
        copied.write_bytes(Path(relabel.__file__).read_bytes())
        command = [sys.executable, str(copied), "--phase-json", str(self.phase_path),
                   "--time-npz", str(self.npz_path), "--output", str(self.output)]
        first = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        copied.write_text(copied.read_text() + "\n# changed implementation identity\n")
        second = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(second.returncode, 2)
        self.assertIn("new output version", second.stderr)

    def test_tampered_or_missing_output_is_not_reused(self):
        self.run_product()
        path = self.output / relabel.NPZ_NAME
        path.write_bytes(path.read_bytes() + b"tampered")
        with self.assertRaisesRegex(relabel.RelabelError, "checksum mismatch"):
            self.run_product()
        path.unlink()
        with self.assertRaisesRegex(relabel.RelabelError, "missing"):
            self.run_product()

    def test_write_failure_does_not_publish_success_and_can_retry(self):
        with mock.patch.object(relabel, "_write_json", side_effect=OSError("simulated full disk")):
            with self.assertRaisesRegex(OSError, "full disk"):
                self.run_product()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".candidate_v1.partial-*")), [])
        self.assertEqual(self.run_product()["status"], "created")

    def test_existing_uncommitted_directory_is_preserved_and_empty_directory_can_be_used(self):
        self.output.mkdir()
        self.assertEqual(self.run_product()["status"], "created")
        self.output = self.root / "preexisting"
        self.output.mkdir()
        marker = self.output / "unrelated.txt"
        marker.write_text("preserve this")
        with self.assertRaisesRegex(relabel.RelabelError, "not a verified complete product"):
            self.run_product()
        self.assertEqual(marker.read_text(), "preserve this")

    def test_empty_phase_cli_writes_explicit_unknown_report(self):
        self.phase_path.write_text(json.dumps(phase()))
        command = [sys.executable, str(Path(relabel.__file__)), "--phase-json", str(self.phase_path),
                   "--time-npz", str(self.npz_path), "--output", str(self.output)]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["counts"]["candidate_present"], 0)
        report = json.loads((self.output / relabel.REPORT_NAME).read_text())
        self.assertEqual(report["counts"]["unknown"], 300)
        self.assertFalse(report["training_approved"])


if __name__ == "__main__":
    unittest.main()

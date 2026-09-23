from __future__ import annotations

import unittest

from pico_f1.camera_sync import (
    CameraFrameStamp,
    CameraSyncGate,
)


KEYS = ("head", "left", "right")


def frames(sequences=(1, 1, 1), sources=(1, 10, 100), arrivals=(20, 21, 22)):
    return {
        key: CameraFrameStamp(sequence, source, arrival)
        for key, sequence, source, arrival in zip(
            KEYS, sequences, sources, arrivals
        )
    }


class CameraSyncGateTest(unittest.TestCase):
    def test_continuous_capture_reports_gaps_without_claiming_integrity(self):
        gate = CameraSyncGate(KEYS, 0.05, 0.055, allow_source_gaps=True)
        gate.arm(frames(sequences=(0, 0, 0)))
        first = {key: CameraFrameStamp(1, 100_000_000, 100_000_000) for key in KEYS}
        self.assertTrue(gate.evaluate(first).ready)
        after_gap = {key: CameraFrameStamp(2, 166_666_666, 166_666_666) for key in KEYS}
        decision = gate.evaluate(after_gap)
        self.assertTrue(decision.ready)
        self.assertEqual(len(decision.source_gaps), 3)
        following = {key: CameraFrameStamp(3, 200_000_000, 200_000_000) for key in KEYS}
        self.assertEqual(gate.evaluate(following).source_gaps, ())

    def setUp(self) -> None:
        self.gate = CameraSyncGate(
            KEYS, max_sync_skew_s=0.05, max_source_gap_s=0.05
        )
        # Tests use small integer nanoseconds, so override converted limits.
        self.gate.max_sync_skew_ns = 5
        self.gate.max_source_gap_ns = 50

    def test_cross_camera_source_clocks_need_not_share_an_epoch(self) -> None:
        self.gate.arm(frames(sequences=(0, 0, 0)))
        decision = self.gate.evaluate(frames())
        self.assertTrue(decision.ready)
        self.assertFalse(decision.fatal)

    def test_each_camera_frame_is_consumed_exactly_once(self) -> None:
        self.gate.arm(frames(sequences=(0, 0, 0)))
        self.assertTrue(self.gate.evaluate(frames()).ready)
        self.assertFalse(self.gate.evaluate(frames()).ready)
        fresh = frames(
            sequences=(2, 2, 2), sources=(2, 11, 101), arrivals=(30, 31, 32)
        )
        self.assertTrue(self.gate.evaluate(fresh).ready)

    def test_skew_waits_for_lagging_stream_without_consuming(self) -> None:
        self.gate.arm(frames(sequences=(0, 0, 0)))
        skewed = frames(arrivals=(20, 21, 40))
        self.assertIn("source-aligned", self.gate.evaluate(skewed).reason)
        aligned = frames(arrivals=(38, 39, 40))
        self.assertTrue(self.gate.evaluate(aligned).ready)

    def test_duplicate_or_missing_source_frame_is_fatal(self) -> None:
        self.gate.arm(frames(sequences=(0, 0, 0)))
        self.assertTrue(self.gate.evaluate(frames()).ready)
        duplicate = frames(
            sequences=(2, 2, 2), sources=(1, 11, 101), arrivals=(30, 31, 32)
        )
        self.assertTrue(self.gate.evaluate(duplicate).fatal)

        gate = CameraSyncGate(KEYS, 0.05, 0.05)
        gate.max_sync_skew_ns = 5
        gate.max_source_gap_ns = 50
        gate.arm(frames(sequences=(0, 0, 0)))
        self.assertTrue(gate.evaluate(frames()).ready)
        missing = frames(
            sequences=(2, 2, 2), sources=(100, 11, 101), arrivals=(30, 31, 32)
        )
        self.assertTrue(gate.evaluate(missing).fatal)


if __name__ == "__main__":
    unittest.main()

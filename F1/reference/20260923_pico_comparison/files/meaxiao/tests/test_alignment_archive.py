from collections import deque
from pathlib import Path
import tempfile
import unittest

import numpy as np

from pico_f1.alignment_archive import archive_frame, read_extras, validate_coverage
from pico_f1.camera_sync import CameraFrameStamp, CameraSyncGate, align_frame_queues
from pico_f1.lerobot_v3 import CAMERA_KEYS


class AlignmentTest(unittest.TestCase):
    def test_twenty_minute_independent_clocks_do_not_deadlock(self):
        rates = (29.964, 29.993, 30.005)
        events = sorted((int(1e9 + n / rate * 1e9), key, n + 1)
                        for key, rate in zip(CAMERA_KEYS, rates)
                        for n in range(int(1200 * rate)))
        queues = {key: deque() for key in CAMERA_KEYS}
        pending = {key: [] for key in CAMERA_KEYS}
        gate = CameraSyncGate(CAMERA_KEYS, 0.05, 0.055)
        gate.arm({key: CameraFrameStamp(0, 1, 1) for key in CAMERA_KEYS})
        count = extra_count = 0
        for stamp, key, sequence in events:
            queues[key].append((b"jpeg", stamp, stamp, 64, 48, sequence, stamp))
            selected, extras = align_frame_queues(queues, 50_000_000)
            for camera, frame in extras:
                pending[camera].append(CameraFrameStamp(frame[5], frame[6], frame[1]))
                extra_count += 1
            if selected:
                decision = gate.evaluate({camera: CameraFrameStamp(f[5], f[6], f[1])
                                          for camera, f in selected.items()}, pending)
                self.assertTrue(decision.ready, decision.reason)
                for camera in CAMERA_KEYS:
                    queues[camera].popleft()
                    pending[camera].clear()
                count += 1
            self.assertLess(max(map(len, queues.values())), 5)
        self.assertGreater(count, 35900)
        self.assertGreater(extra_count, 50)

    def test_real_gap_cannot_be_hidden_without_source_image(self):
        sources = np.asarray([[100_000_000] * 3, [166_666_666] * 3])
        with self.assertRaisesRegex(ValueError, "frame gap"):
            validate_coverage(sources, [], 55_000_000)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for key in CAMERA_KEYS:
                archive_frame(root, key, (b"jpeg", 133_333_333, 2, 64, 48, 2, 133_333_333), "clock_alignment")
            validate_coverage(sources, read_extras(root), 55_000_000)
            (root / CAMERA_KEYS[0] / "000000000002.jpg").unlink()
            with self.assertRaises(OSError):
                read_extras(root)

    def test_camera_skew_advances_heads_instead_of_waiting_forever(self):
        queues = {key: deque([(b"jpeg", stamp, stamp, 64, 48, n, stamp)
                              for n, stamp in enumerate(stamps)])
                  for key, stamps in zip(CAMERA_KEYS, ([1, 34, 67, 100], [100], [100]))}
        selected, extras = align_frame_queues(queues, 50)
        self.assertEqual(len(extras), 2)
        self.assertEqual(selected[CAMERA_KEYS[0]][1], 67)

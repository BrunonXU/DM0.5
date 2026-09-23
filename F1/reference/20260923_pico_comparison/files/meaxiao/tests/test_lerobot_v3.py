from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import numpy as np
import pyarrow.parquet as pq

from pico_f1.lerobot_v3 import (
    CAMERA_KEYS,
    FEATURE_NAMES,
    FEATURE_SIZE,
    LeRobotV3Dataset,
    jpeg_dimensions,
)


class LeRobotV3WriterTest(unittest.TestCase):
    def _video(self, path: Path, color: str) -> None:
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"color={color}:s=64x48:r=30",
                "-frames:v", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                str(path),
            ],
            check=True,
        )

    def _episode(self, root: Path, offset: float):
        staging = root / ".staging" / f"episode-{int(offset)}"
        staging.mkdir(parents=True)
        for key, color in zip(CAMERA_KEYS, ("red", "green", "blue")):
            self._video(staging / f"{key}.mp4", color)
        state = np.vstack(
            [
                np.arange(FEATURE_SIZE, dtype=np.float32) + offset,
                np.arange(FEATURE_SIZE, dtype=np.float32) + offset + 1.0,
            ]
        )
        base_ns = 1_000_000_000 + int(offset) * 100_000_000
        step_ns = 33_333_333
        capture_ns = np.asarray([base_ns, base_ns + step_ns], dtype=np.int64)
        arrays = {
            "state": state,
            "action": state + 0.25,
            "timestamp_ns": capture_ns + 10_000_000_000,
            "camera_timestamp_ns": np.asarray(
                [
                    [base_ns, base_ns + 1_000_000, base_ns + 2_000_000],
                    [
                        base_ns + step_ns,
                        base_ns + step_ns + 1_000_000,
                        base_ns + step_ns + 2_000_000,
                    ],
                ],
                dtype=np.int64,
            ),
            "camera_integrity_timestamp_ns": np.asarray(
                [
                    [base_ns, base_ns + 1_000_000, base_ns + 2_000_000],
                    [
                        base_ns + step_ns,
                        base_ns + step_ns + 1_000_000,
                        base_ns + step_ns + 2_000_000,
                    ],
                ],
                dtype=np.int64,
            ),
            "capture_monotonic_ns": capture_ns,
            "camera_arrival_timestamp_ns": np.asarray(
                [
                    [base_ns - 2_000_000, base_ns - 1_000_000, base_ns],
                    [
                        base_ns + step_ns - 2_000_000,
                        base_ns + step_ns - 1_000_000,
                        base_ns + step_ns,
                    ],
                ],
                dtype=np.int64,
            ),
            "state_max_age_ns": np.asarray(
                [5_000_000, 6_000_000], dtype=np.int64
            ),
            "action_max_age_ns": np.asarray(
                [4_000_000, 5_000_000], dtype=np.int64
            ),
        }
        camera_info = {key: {"width": 64, "height": 48} for key in CAMERA_KEYS}
        return staging, arrays, camera_info

    def test_append_two_complete_v3_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "dataset"
            dataset = LeRobotV3Dataset(root, fps=30.0, task="synthetic")
            for offset in (0.0, 10.0):
                staging, arrays, camera_info = self._episode(root, offset)
                dataset.commit(staging, arrays, camera_info)

            info = json.loads((root / "meta/info.json").read_text())
            stats = json.loads((root / "meta/stats.json").read_text())
            self.assertEqual(info["codebase_version"], "v3.0")
            self.assertEqual(info["total_episodes"], 2)
            self.assertEqual(info["total_frames"], 4)
            self.assertEqual(info["robot_type"], "f1_omnipicker")
            self.assertEqual(
                info["camera_integrity_basis"][CAMERA_KEYS[0]],
                "source",
            )
            self.assertEqual(len(info["features"]["action"]["names"]), FEATURE_SIZE)
            self.assertEqual(tuple(info["features"]["action"]["names"]), FEATURE_NAMES)
            self.assertEqual(set(CAMERA_KEYS), {
                key for key, value in info["features"].items()
                if value["dtype"] == "video"
            })
            for feature in ("observation.state", "action"):
                self.assertEqual(
                    set(stats[feature]),
                    {"min", "max", "mean", "std", "count", "q01", "q10", "q50", "q90", "q99"},
                )
                self.assertEqual(stats[feature]["count"], [4])

            episode_rows = []
            for path in sorted((root / "meta/episodes").glob("*/*.parquet")):
                episode_rows.extend(pq.read_table(path).to_pylist())
            self.assertEqual([row["episode_index"] for row in episode_rows], [0, 1])
            self.assertEqual([row["dataset_from_index"] for row in episode_rows], [0, 2])
            self.assertEqual([row["dataset_to_index"] for row in episode_rows], [2, 4])
            for key in CAMERA_KEYS:
                self.assertEqual(len(list((root / "videos" / key).glob("*/*.mp4"))), 2)

    def test_jpeg_dimensions_without_decoder_dependency(self) -> None:
        completed = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=black:s=80x60",
                "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            ],
            check=True,
            capture_output=True,
        )
        self.assertEqual(jpeg_dimensions(completed.stdout), (80, 60))
        with self.assertRaisesRegex(ValueError, "not a JPEG"):
            jpeg_dimensions(b"not-an-image")

    def test_alignment_sidecar_survives_commit(self):
        from pico_f1.alignment_archive import archive_frame, read_extras
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = LeRobotV3Dataset(root, fps=30.0, task="synthetic")
            staging, arrays, camera_info = self._episode(root, 0.0)
            key = CAMERA_KEYS[0]
            stamp = int(arrays["camera_timestamp_ns"][0, 0]) - 33_333_333
            archive_frame(staging / "alignment", key, (b"jpeg", stamp, stamp, 64, 48, 1, stamp), "clock_alignment")
            dataset.commit(staging, arrays, camera_info)
            self.assertEqual(len(read_extras(root / "alignment/episode-000000")), 1)

    def test_missing_source_frame_cannot_enter_training_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = LeRobotV3Dataset(root, fps=30.0, task="synthetic")
            staging, arrays, camera_info = self._episode(root, 0.0)
            arrays["camera_timestamp_ns"][1] += 33_333_333
            with self.assertRaisesRegex(ValueError, "frame gap"):
                dataset.commit(staging, arrays, camera_info)
            self.assertFalse((root / "meta/info.json").exists())
            self.assertTrue(staging.exists())


if __name__ == "__main__":
    unittest.main()

"""Local release packaging checks; no SSH, data transfer or training."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "stage_h20", Path(__file__).resolve().parents[1] / "scripts/stage_h20.py")
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)


class StageH20Tests(unittest.TestCase):
    def test_code_packet_preserves_runtime_assets_without_generated_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            task = projects / "tasks/F1_ComplexyToys"
            task_kept = [
                "README.md", "configs/sft.yaml", "scripts/train_entry.py",
                "src/f1_toys/dataset.py", "src/f1_toys/native/ccs_m6_40.MvKDCfg",
                "tests/fixtures/contract.json", "DataPrehandle/timing_audit.py",
                "DataPrehandle/visualizations/preview/README.md", "Eval/README.md",
                "inference/visualizations/README.md", "artifacts/README.md",
            ]
            task_omitted = [
                "DataPrehandle/visualizations/preview/contact.png",
                "DataPrehandle/visualizations/preview/preview.json",
                "Eval/rollouts/session.MP4", "Eval/rollouts/frames.pack",
                "Eval/artifacts/evidence/README.md", "artifacts/model.safetensors",
                "artifacts/h20-staging/old-release/README.md",
                "inference/visualizations/console.log", "scripts/__pycache__/entry.pyc",
                ".pytest_cache/README.md", ".git/config", "src/stray.pyc",
            ]
            runtime_kept = ["RLinf/rlinf/model.py", "RLinf/rlinf/native/kernel.bin",
                            "RLinf/examples/sft/run.sh", "RLinf/LICENSE"]
            for relative in task_kept + task_omitted:
                path = task/relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(relative.encode())
            for relative in runtime_kept:
                path = projects/relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(relative.encode())
            (task/"linked-model").symlink_to(task/"artifacts/model.safetensors")
            (task/"linked-artifacts").symlink_to(task/"artifacts", target_is_directory=True)
            packet = task / "artifacts/h20-staging/new-release"
            with patch.object(stage, "PROJECTS", projects), patch.object(stage, "TASK", task), \
                    patch.object(stage, "remote", side_effect=AssertionError("Unexpected SSH")):
                manifest = stage.build_packet(packet)
            expected = {str(Path("tasks/F1_ComplexyToys")/name) for name in task_kept} | set(runtime_kept)
            self.assertEqual(set(manifest), expected)
            for name, digest in manifest.items():
                self.assertEqual((packet/name).read_bytes(), (projects/name).read_bytes())
                self.assertEqual(digest, hashlib.sha256((projects/name).read_bytes()).hexdigest())
            self.assertTrue((task/"artifacts/model.safetensors").is_file())
            self.assertTrue((task/"artifacts/h20-staging/old-release/README.md").is_file())

    def test_existing_packet_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            packet = Path(tmp)/"frozen-release"
            packet.mkdir()
            sentinel = packet/"original"
            sentinel.write_bytes(b"frozen")
            with self.assertRaises(FileExistsError):
                stage.build_packet(packet)
            self.assertEqual(sentinel.read_bytes(), b"frozen")


if __name__ == "__main__":
    unittest.main()

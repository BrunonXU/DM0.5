"""Offline security and lifecycle checks; no SSH, ROS or robot connection."""
import getpass
import importlib.util
import io
import json
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("sync_project", Path(__file__).parents[1] / "deploy/sync_project.py")
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class StaticDeployTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.deploy_base = self.base / "robot/Bruno"
        self.target = self.deploy_base / sync.TASK
        base_patch = patch.object(sync, "DEPLOY_BASE", str(self.deploy_base))
        base_patch.start()
        self.addCleanup(base_patch.stop)
        self.workspace = self.base / "source"
        self.task = self.workspace / sync.TASK
        for name in ("PROJECT_RULES_AND_PREFERENCES.md", "F1/AGENTS.md", "F1/README.md",
                     "F1/F1系统配置.md", "F1/F1系统接口清单.md", "F1/通用机器人软件接口文档.md",
                     sync.TASK + "/models/DM0.5/README.md", sync.TASK + "/robot/README.md",
                     sync.TASK + "/scripts/example.py", "F1/docs/connection.md", "F1/reference/README.md"):
            path = self.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# reference\n", encoding="utf-8")

    def package(self, release_id="test-v1"):
        output = self.base / release_id
        sync.prepare(self.workspace, release_id, output)
        return output

    def remote(self, operation, package, target=None):
        target = target if target is not None else self.target
        manifest_data = (package / "manifest.json").read_bytes()
        release_id = json.loads(manifest_data)["release_id"]
        args = [sys.executable, "-c", sync.remote_program(), operation, str(target), release_id,
                platform.node(), getpass.getuser(), platform.machine(), sync.digest(manifest_data)]
        data = (package / "package.tar.gz").read_bytes() if operation == "deploy" else None
        return subprocess.run(args, input=data, capture_output=True), target

    def test_round_trip_filters_and_source_comparison(self):
        for name in ("results/videos/large.mp4", "results/giant_index.json", "models/DM0.5/model.pt", ".env"):
            path = self.task / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x00binary")
        package = self.package()
        manifest, _ = sync.load_package(package, compare_source=True)
        self.assertIn(sync.TASK + "/models/DM0.5/README.md", {f["path"] for f in manifest["files"]})
        self.assertEqual(len(manifest["files"]), 11)
        (self.task / "scripts/example.py").write_text("# changed\n")
        with self.assertRaisesRegex(ValueError, "Source differs"):
            sync.load_package(package, compare_source=True)
        sync.load_package(package)  # Frozen contents remain valid.

    def test_source_symlink_is_rejected(self):
        (self.task / "scripts/alias.py").symlink_to(self.task / "scripts/example.py")
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.package()

    def test_tar_traversal_links_duplicates_are_rejected(self):
        for name, kind in (("../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
                           ("workspace/link", tarfile.SYMTYPE), ("workspace/hard", tarfile.LNKTYPE),
                           (".", tarfile.REGTYPE)):
            with self.subTest(name=name):
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode="w:gz") as tar:
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    info.linkname = "../outside"
                    tar.addfile(info, io.BytesIO())
                data.seek(0)
                with self.assertRaises(ValueError):
                    sync.validate_archive(data)
        duplicate = io.BytesIO()
        with tarfile.open(fileobj=duplicate, mode="w:gz") as tar:
            for _ in range(2):
                tar.addfile(tarfile.TarInfo("manifest.json"), io.BytesIO())
        duplicate.seek(0)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            sync.validate_archive(duplicate)

    def test_package_tampering_is_rejected(self):
        package = self.package()
        with (package / "package.tar.gz").open("ab") as handle:
            handle.write(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            sync.load_package(package)

    def test_first_deployment_relative_layout_and_idempotence(self):
        package = self.package()
        before, target = self.remote("preflight", package)
        self.assertEqual(before.returncode, 0, before.stderr.decode())
        self.assertFalse(json.loads(before.stdout)["target_exists"])
        result, _ = self.remote("deploy", package)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        report = json.loads(result.stdout)
        self.assertEqual(report["file_count"], 11)
        self.assertTrue(report["active"])
        self.assertFalse(report["robot_control_started"])
        self.assertEqual((target / "current/../AGENTS.md").read_text(), "# reference\n")
        self.assertTrue((target / "models/DM0.5/README.md").is_file())
        self.assertFalse((target / "current/.venv").exists())
        again, _ = self.remote("deploy", package)
        self.assertEqual(again.returncode, 0, again.stderr.decode())
        self.assertTrue(json.loads(again.stdout)["idempotent_verify_only"])
        verified, _ = self.remote("verify", package)
        self.assertEqual(verified.returncode, 0, verified.stderr.decode())

    def test_foreign_contents_at_allowed_root_are_preserved(self):
        package = self.package()
        foreign = self.target
        foreign.mkdir(parents=True)
        (foreign / "site.txt").write_text("keep")
        rejected, _ = self.remote("deploy", package, foreign)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(b"not a completed managed deployment", rejected.stderr)
        self.assertEqual((foreign / "site.txt").read_text(), "keep")
        self.assertEqual(sorted(p.name for p in foreign.iterdir()), ["site.txt"])

    def test_existing_different_release_is_preserved(self):
        package = self.package()
        deployed, target = self.remote("deploy", package)
        self.assertEqual(deployed.returncode, 0, deployed.stderr.decode())
        second = self.package("test-v2")
        rejected, _ = self.remote("deploy", second)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(b"backup", rejected.stderr)
        self.assertFalse((target / "releases/test-v2").exists())
        self.assertIn("test-v1", str((target / "current").readlink()))

    def test_local_and_remote_reject_targets_outside_exact_task_root(self):
        package = self.package()
        invalid_targets = (
            "/home/user/projects/" + sync.TASK,
            str(self.base / "robot/projects" / sync.TASK),
            str(self.base / "robot/Bruno-other" / sync.TASK),
            str(self.deploy_base) + "/../Bruno/" + sync.TASK,
            str(self.target) + "/.",
            str(self.target) + "//",
            str(self.deploy_base / "F1/another_task"),
            "Bruno/" + sync.TASK,
        )
        for target in invalid_targets:
            with self.subTest(target=target):
                # Rejection occurs while building argv, before any SSH process.
                args = SimpleNamespace(target_root=target)
                with self.assertRaisesRegex(ValueError, "target must be exactly"):
                    sync.ssh_command(args, "deploy", "test-v1", "0" * 64)
                rejected, _ = self.remote("deploy", package, target)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn(b"target must be exactly", rejected.stderr)
        self.assertFalse(self.deploy_base.parent.exists())

    def test_remote_rejects_target_ancestor_symlink(self):
        package = self.package()
        outside = self.base / "existing_other_workspace"
        outside.mkdir()
        (outside / "site.txt").write_text("keep")
        self.deploy_base.parent.mkdir(parents=True)
        self.deploy_base.symlink_to(outside, target_is_directory=True)
        for operation in ("preflight", "deploy", "verify"):
            with self.subTest(operation=operation):
                rejected, _ = self.remote(operation, package)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn(b"Symlink", rejected.stderr)
        self.assertEqual((outside / "site.txt").read_text(), "keep")
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["site.txt"])
        self.assertTrue(self.deploy_base.is_symlink())

    def test_field_edits_and_added_links_are_detected_without_overwrite(self):
        package = self.package()
        deployed, target = self.remote("deploy", package)
        self.assertEqual(deployed.returncode, 0, deployed.stderr.decode())
        field_file = target / "current/scripts/example.py"
        field_file.write_text("# field change\n")
        rejected, _ = self.remote("deploy", package)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(field_file.read_text(), "# field change\n")
        field_file.write_text("# reference\n")
        (target / "current/robot/extra.py").symlink_to(self.task / "scripts/example.py")
        rejected, _ = self.remote("verify", package)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(b"Symlink", rejected.stderr)


if __name__ == "__main__":
    unittest.main()

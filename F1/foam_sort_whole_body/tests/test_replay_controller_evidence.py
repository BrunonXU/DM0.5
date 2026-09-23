"""Version-bound controller evidence tests; fake /proc only, no robot or ROS."""
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "robot"))
from f1_replay import controller_evidence as evidence


class ControllerEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.exe = self.root / "motor_ctl_node"
        self.exe.write_bytes(b"synthetic executable, never run")
        self.profile = {"joint_limit_source": "verified_controller_binary",
                        "controller_binary_sha256": evidence.AUDITED_SHA256,
                        "joint_limits_deg": {"arm_l_j1": [-999, 999]}}

    def process(self, pid, node="motor_ctl"):
        process = self.proc / str(pid)
        process.mkdir()
        (process / "comm").write_text("motor_ctl_node\n")
        (process / "cmdline").write_bytes(b"motor_ctl_node\0--ros-args\0-r\0__node:=" + node.encode() + b"\0")
        (process / "stat").write_text(f"{pid} (motor_ctl_node) S " + "0 " * 18 + "1000")
        (process / "exe").symlink_to(self.exe)

    def test_matching_version_uses_registry_never_profile_limits(self):
        self.process(123)
        with patch.object(evidence, "_sha256", return_value=evidence.AUDITED_SHA256):
            result = evidence.read_verified_binary_limits(self.profile, self.proc)
        self.assertEqual(result["parameters"]["arm_l_j1_min"], -178)
        self.assertEqual(result["parameters"]["arm_r_j2_max"], 120)
        self.assertEqual(len(result["parameters"]), 28)
        self.assertEqual(result["pid"], 123)

    def test_unknown_live_binary_and_unreviewed_profile_version_are_rejected(self):
        self.process(123)
        with self.assertRaisesRegex(RuntimeError, "SHA256"):
            evidence.read_verified_binary_limits(self.profile, self.proc)
        self.profile["controller_binary_sha256"] = hashlib.sha256(self.exe.read_bytes()).hexdigest()
        with self.assertRaisesRegex(RuntimeError, "不在已审计"):
            evidence.read_verified_binary_limits(self.profile, self.proc)

    def test_new_robot_cannot_accept_old_robot_binary(self):
        self.process(123)
        self.profile['controller_binary_sha256'] = evidence.NEW_F1_SHA256
        with patch.object(evidence, '_sha256', return_value=evidence.AUDITED_SHA256):
            with self.assertRaisesRegex(RuntimeError, 'SHA256'):
                evidence.read_verified_binary_limits(self.profile, self.proc)
        with patch.object(evidence, '_sha256', return_value=evidence.NEW_F1_SHA256):
            result = evidence.read_verified_binary_limits(self.profile, self.proc)
        self.assertEqual(result['sha256'], evidence.NEW_F1_SHA256)
        self.assertIn('20260918-new-f1-29-v1', result['reference'])
        self.assertEqual(result['parameters']['arm_l_j2_min'], -94)

    def test_multiple_processes_or_wrong_node_are_rejected(self):
        self.process(123, node="different_motor_ctl")
        with self.assertRaisesRegex(RuntimeError, "节点名"):
            evidence.read_verified_binary_limits(self.profile, self.proc)
        self.process(124)
        with self.assertRaisesRegex(RuntimeError, "实际2个"):
            evidence.read_verified_binary_limits(self.profile, self.proc)


if __name__ == "__main__":
    unittest.main()

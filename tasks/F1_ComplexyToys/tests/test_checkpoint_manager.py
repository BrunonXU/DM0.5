"""Deployment failure paths; no SSH, Docker or robot I/O."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import io
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location("checkpoint_manager", Path(__file__).resolve().parents[1] / "scripts/checkpoint.py")
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


class CheckpointSwitchTests(unittest.TestCase):
    def run_failure(self, occupied):
        with tempfile.TemporaryDirectory() as tmp:
            process = MagicMock()
            process.poll.return_value = 1
            process.stdin.closed = False
            with patch.object(manager, "ART", Path(tmp)), \
                 patch.object(manager, "ensure_policy_tunnel"), \
                 patch.object(manager, "verify", return_value="new"), \
                 patch.object(manager, "wait_health", return_value={}), \
                 patch.object(manager, "docker") as docker, \
                 patch.object(manager, "launch", side_effect=[None, RuntimeError("load failure")]) as launch, \
                 patch.object(manager.subprocess, "Popen", return_value=process), \
                 patch.object(manager.subprocess, "run") as rollback, \
                 patch.object(manager, "line", side_effect=RuntimeError("occupied") if occupied else [{"locked": True}]):
                with self.assertRaises(RuntimeError):
                    manager.activate(500, Path(tmp))
                calls = [c.args for c in docker.call_args_list]
                if occupied:
                    self.assertNotIn(("stop", "f1-toys-eef-1000-5step"), calls)
                    self.assertEqual(launch.call_count, 1)
                    rollback.assert_not_called()
                    self.assertFalse((Path(tmp) / "active-checkpoint.json").exists())
                else:
                    self.assertIn(("stop", "f1-toys-eef-1000-5step"), calls)
                    self.assertIn(("start", "f1-toys-eef-1000-5step"), calls)
                    self.assertEqual(json.loads((Path(tmp) / "active-checkpoint.json").read_text())["container"], "f1-toys-eef-1000-5step")
                    rollback.assert_called_once()

    def test_occupied_control_does_not_stop_current_service(self):
        self.run_failure(True)

    def test_failed_new_service_restores_previous_service(self):
        self.run_failure(False)

    def test_bundle_integrity_failure_rejects_before_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "model").write_bytes(b"changed")
            (p / "bundle.json").write_text(json.dumps({"files_sha256": {"model": "wrong"}}))
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                manager.verify(p)

    def test_missing_tunnel_fails_before_touching_containers(self):
        with patch.object(manager, "ensure_policy_tunnel", side_effect=RuntimeError("tunnel unavailable")), \
             patch.object(manager, "docker") as docker, patch.object(manager, "launch") as launch:
            with self.assertRaisesRegex(RuntimeError, "tunnel unavailable"):
                manager.activate(7000, Path('/unused'))
            docker.assert_not_called()
            launch.assert_not_called()

    def test_remote_health_error_is_not_misreported_as_busy_control(self):
        process = MagicMock()
        process.stdout = io.StringIO(json.dumps({'stage':'tunnel_health','error':'URLError: Connection refused'})+'\n')
        with patch.object(manager.select, 'select', return_value=([process.stdout], [], [])):
            with self.assertRaisesRegex(RuntimeError, r'\[tunnel_health\].*Connection refused'):
                manager.line(process)

    def test_remote_binding_commit_and_disconnect_rollback(self):
        for commit in (True, False):
            with self.subTest(commit=commit), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = root / 'config.json'
                original = json.dumps({'policy': {'model_id':'old','chunk_steps':20,'frequency_hz':30}})
                config.write_text(original)
                code = manager.REMOTE_BIND.replace('/home/user/Bruno/F1/runtime/control.lock', str(root/'lock'))
                code = code.replace('/home/user/Bruno/DAgger-runtime/f1-toys-20261016/config-standalone.json', str(config))
                health = {'model_id':'new','protocol':'f1_eef_sync_v2','denoise_steps':5}
                incoming = json.dumps({'health':health})+'\n'+('COMMIT\n' if commit else '')
                namespace = {}
                with patch('sys.stdin', io.StringIO(incoming)), patch('sys.stdout', io.StringIO()), \
                     patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(health).encode())):
                    try:
                        exec(code, namespace)
                    finally:
                        if 'lock' in namespace: namespace['lock'].close()
                if commit:
                    self.assertEqual(json.loads(config.read_text())['policy']['model_id'], 'new')
                else:
                    self.assertEqual(config.read_text(), original)


if __name__ == "__main__":
    unittest.main()

import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pico_upper.supervisor import process_start, stop, wait_for_enter, terminate_owned_groups

class StopIdentityTest(unittest.TestCase):
    def test_current_start_time_is_readable(self):
        self.assertTrue(process_start(os.getpid()).isdigit())
    def test_stale_pid_never_signals_reused_process(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'session.json').write_text(json.dumps({'pid':os.getpid(),'process_start':'stale'}))
            with patch('os.kill') as kill:
                self.assertEqual(stop(root),1)
                kill.assert_not_called()
    def test_matching_start_time_but_wrong_module_is_not_killed(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'session.json').write_text(json.dumps({'pid':os.getpid(),'process_start':process_start(os.getpid())}))
            with patch('os.kill') as kill:
                with self.assertRaisesRegex(RuntimeError,'identity mismatch'):
                    stop(root)
                kill.assert_not_called()
    def test_missing_session_does_not_touch_any_process(self):
        with tempfile.TemporaryDirectory() as d, patch('os.kill') as kill:
            self.assertEqual(stop(Path(d)),0)
            kill.assert_not_called()
class ConfirmationCancellationTest(unittest.TestCase):
    def test_stop_cancels_prompt_without_enter(self):
        read_fd, write_fd = os.pipe()
        cancelled = threading.Event()
        timer = threading.Timer(.04, cancelled.set)
        timer.start()
        try:
            with os.fdopen(read_fd) as stream:
                start = time.monotonic()
                with self.assertRaises(KeyboardInterrupt):
                    wait_for_enter(stream, cancelled.is_set)
                self.assertLess(time.monotonic()-start, .5)
        finally:
            os.close(write_fd)
            timer.join()
    def test_enter_accepts_and_closed_terminal_rejects(self):
        for data in (b'\n', b''):
            read_fd, write_fd = os.pipe()
            os.write(write_fd, data); os.close(write_fd)
            with os.fdopen(read_fd) as stream:
                if data:
                    wait_for_enter(stream, lambda: False)
                else:
                    with self.assertRaisesRegex(RuntimeError,'TERMINAL_CLOSED'):
                        wait_for_enter(stream, lambda: False)
    def test_cleanup_terminates_helpers_after_leader_exit(self):
        leader = SimpleNamespace(pid=1234, poll=lambda:0)
        with patch('os.killpg') as signal_group:
            terminate_owned_groups([leader])
            signal_group.assert_called_once()
            self.assertEqual(signal_group.call_args.args[0],1234)

if __name__=='__main__':unittest.main()

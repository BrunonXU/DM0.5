"""Native startup overlaps services without weakening group lifecycle semantics."""
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest

from f1_harness import native, site


def test_native_services_selects_parallel_supervision(tmp_path, monkeypatch):
    calls = []
    commands = [dict(name=name, argv=['unused']) for name in ('numerics', 'plan', 'verify', 'gateway')]
    monkeypatch.setattr(site, 'service_commands', lambda *a, **kw: commands)
    def supervise(actual, run, **kwargs):
        calls.append((actual, run, kwargs))
        return dict(state='stopped', motion_authorized=False)
    monkeypatch.setattr(site, 'supervise', supervise)
    run = tmp_path / 'native-run'
    monkeypatch.setattr(sys, 'argv', ['native', '--run', str(run), 'services', str(tmp_path / 'site')])
    assert native.main() == 0
    assert calls == [(commands, run / 'processes', dict(parallel=True))]
    assert json.loads((run / 'result.json').read_text())['motion_authorized'] is False


def _command(name, body):
    return dict(name=name, argv=[sys.executable, '-c', body])


def _assert_reaped(run):
    receipt = json.loads((run / 'services.json').read_text())
    for row in receipt['services']:
        assert row['returncode'] is not None
        with pytest.raises(ProcessLookupError):
            os.kill(row['pid'], 0)
    return receipt


def test_parallel_dependency_exit_before_first_ready_reaps_every_child(tmp_path):
    run = tmp_path / 'run'
    commands = [_command('slow_numerics', 'import time; time.sleep(30)'),
                _command('failed_gateway', 'raise SystemExit(9)')]
    with pytest.raises(RuntimeError, match='dependency_exited_during_startup'):
        site.supervise(commands, run, parallel=True, startup_timeout=2)
    receipt = _assert_reaped(run)
    assert receipt['state'] == 'failed'
    assert len(receipt['services']) == 2


def test_cancel_during_parallel_initialization_reaps_all_without_ready(tmp_path, capsys):
    run = tmp_path / 'run'
    stop = threading.Event()
    marker = tmp_path / 'last-service-started'
    commands = [_command('numerics', 'import time; time.sleep(30)'),
                _command('gateway', f'from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(30)')]
    def cancel_after_both_started():
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        stop.set()
    waiter = threading.Thread(target=cancel_after_both_started)
    waiter.start()
    try:
        receipt = site.supervise(commands, run, parallel=True, stop=stop, startup_timeout=4)
    finally:
        stop.set()
        waiter.join(timeout=4)
    assert marker.exists()
    assert receipt['state'] == 'stopped'
    assert len(receipt['services']) == 2
    assert '"ready": true' not in capsys.readouterr().out
    _assert_reaped(run)

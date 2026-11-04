"""Independent gateway lifecycle and access checks; worker launch stays mocked."""
import itertools
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recording_service
from recording_service import RecordingService, confined_file
from test_data_auth import http_app


def test_slow_prepare_retry_reuses_live_child_and_still_does_not_record(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    python = runtime / 'recording-venv/bin/python'
    python.parent.mkdir(parents=True); python.touch()
    service = RecordingService(runtime, tmp_path)
    starts = []
    child = NS(poll=lambda: None)
    monkeypatch.setattr(recording_service.subprocess, 'Popen', lambda *a, **kw: starts.append(a) or child)
    ticks = itertools.count()
    monkeypatch.setattr(recording_service.time, 'monotonic', lambda: float(next(ticks)))
    monkeypatch.setattr(recording_service.time, 'sleep', lambda _: None)
    monkeypatch.setattr(recording_service.socket, 'socket', lambda *a: pytest.fail('preparation must not send start'))
    with pytest.raises(ValueError, match='尚未开始采集'):
        service.command('start')
    assert service.state()['mode'] == 'starting'
    states = iter([{'mode': 'starting'}, {'mode': 'starting'}, {'mode': 'idle', 'ready': False}])
    monkeypatch.setattr(service, 'state', lambda: next(states))
    assert service.command('start')['mode'] == 'idle'
    assert len(starts) == 1


def test_read_routes_never_start_or_command_worker(http_app, monkeypatch):
    app, request = http_app
    monkeypatch.setattr(app.recording, 'ensure_worker', lambda: pytest.fail('GET started a worker'))
    monkeypatch.setattr(app.recording, 'command', lambda operation: pytest.fail('GET sent a recorder command'))
    for route in ('/api/recording/state', '/api/data/catalog', '/api/data/recording'):
        assert request(route)[0] == 200
    assert app.recording.child is None and app.lease.fd is None


def test_recording_post_requires_login_and_same_origin_but_no_motion_lease(http_app, monkeypatch):
    app, request = http_app
    called = []
    monkeypatch.setattr(app.recording, 'command', lambda operation: called.append(operation) or {'mode': 'idle'})
    _, _, cookie = request('/api/session')
    assert request('/api/recording/start', {}, cookie, origin=False)[0] == 403
    assert request('/api/login', {}, cookie)[0] == 200
    assert request('/api/recording/start', {}, cookie, origin=False)[0] == 403
    assert called == []
    assert request('/api/recording/start', {}, cookie)[0] == 200
    assert called == ['start']
    assert app.lease.fd is None and app.actions.latest() is None


def test_symlink_ancestor_cannot_escape_media_root(tmp_path):
    real = tmp_path / 'real'; (real / 'nested').mkdir(parents=True)
    (real / 'nested/clip.mp4').write_bytes(b'fixture')
    alias = tmp_path / 'alias'; alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError):
        confined_file(alias / 'nested', 'clip.mp4')

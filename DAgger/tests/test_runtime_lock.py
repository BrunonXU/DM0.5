import os
import pytest
from g2dagger.runtime_lock import check_runtime_available


def test_live_owner_is_reported_without_removing_lock(tmp_path):
    path = tmp_path / 'owner.pid'
    path.write_text(str(os.getpid()))
    with pytest.raises(RuntimeError, match=f'PID {os.getpid()}'):
        check_runtime_available(path)
    assert path.read_text() == str(os.getpid())


def test_absent_owner_is_left_for_atomic_acquisition(tmp_path, monkeypatch):
    path = tmp_path / 'owner.pid'
    check_runtime_available(path)
    path.write_text('123')
    def absent(*args):
        raise ProcessLookupError()
    monkeypatch.setattr(os, 'kill', absent)
    check_runtime_available(path)
    assert path.read_text() == '123'


def test_partial_lock_is_not_deleted(tmp_path):
    path = tmp_path / 'owner.pid'
    path.write_text('')
    with pytest.raises(RuntimeError, match='incomplete/invalid'):
        check_runtime_available(path)
    assert path.exists()

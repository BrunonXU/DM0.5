from types import SimpleNamespace
import pytest
from g2dagger.recording_storage import check_recording_storage


def test_full_volume_fails_with_actual_path_and_reason(tmp_path, monkeypatch):
    monkeypatch.setattr('g2dagger.recording_storage.shutil.disk_usage', lambda p: SimpleNamespace(free=0))
    with pytest.raises(RuntimeError, match='recording disk space low'):
        check_recording_storage([tmp_path / 'new' / 'episode'])


def test_link_is_checked_on_destination_volume(tmp_path, monkeypatch):
    target = tmp_path / 'large_volume'
    target.mkdir()
    link = tmp_path / 'data'
    link.symlink_to(target, target_is_directory=True)
    checked = []
    def usage(path):
        checked.append(path)
        return SimpleNamespace(free=10 * 1024**3)
    monkeypatch.setattr('g2dagger.recording_storage.shutil.disk_usage', usage)
    check_recording_storage([link / 'new_episode'])
    assert checked == [target]
